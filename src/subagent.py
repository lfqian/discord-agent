#!/usr/bin/env python3
"""Sub-agent manager — long-lived background workers that outlive one @-invocation.

Each @-mention runs as a single `claude -p` with a ~30-min cap and no memory
beyond what it reads back. To run work that's longer than that, or to fan a task
out and keep tabs on it across invocations, spawn it as a SUB-AGENT inside a
named **tmux** session: the session keeps running after this `claude -p` exits,
and a later invocation can list it, read its output, send it input, or reap it.

State for each sub-agent lives in `/workspace/subagents/<name>.json` (survives
container restarts), so a fresh CowBot run can rediscover what's running and why.

Usage:
  subagent.py spawn <name> <command...>   # start a detached tmux session
      [--cwd DIR] [--channel CH] [--note "what/why"] [--report]
  subagent.py claude <name> "<prompt>"    # spawn a sub-agent that IS a claude -p
      [--cwd DIR] [--channel CH] [--note ...] [--model M]
  subagent.py list                        # all sessions + status (running/done)
  subagent.py logs <name> [--lines N]     # last N lines of the session's output
  subagent.py send <name> "<keys>"        # type into the session (control it)
  subagent.py wait <name> [--timeout S]   # block until it exits (for short waits)
  subagent.py kill <name>                 # stop + clean up
  subagent.py reap                        # drop state for sessions that ended

Conventions:
- Names are kebab-case task ids, e.g. `video-backfill`, `beta-backtest`.
- `--channel` records where to report; with `--report` a wrapper posts a Discord
  line via /app/src/discord_api.py when the command finishes (exit code included).
- Long jobs: spawn, tell the user "started, will report when done", and check
  `list`/`logs` on a later invocation. Don't block this run waiting.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get(
    "DISCORD_AGENT_WORKSPACE", os.path.expanduser("~/discordAgentWorkspace")
)
# Persist under /workspace so a fresh container/invocation can rediscover jobs.
STATE_DIR = os.path.join(WORKSPACE, "subagents")
LOG_DIR = os.path.join(WORKSPACE, "logs")
SESSION_PREFIX = "sa-"  # tmux session name = SESSION_PREFIX + <name>


def _ensure_dirs():
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def _sess(name):
    return SESSION_PREFIX + name


def _state_path(name):
    return os.path.join(STATE_DIR, name + ".json")


def _log_path(name):
    return os.path.join(LOG_DIR, "subagent-" + name + ".log")


def _tmux(*args, check=False, capture=True):
    return subprocess.run(
        ["tmux", *args],
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=check,
    )


def _alive(name):
    return _tmux("has-session", "-t", _sess(name)).returncode == 0


def _load_state(name):
    try:
        with open(_state_path(name)) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(name, **fields):
    _ensure_dirs()
    st = _load_state(name)
    st.update(fields)
    st["name"] = name
    tmp = _state_path(name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, _state_path(name))


def _write_runner(command, name, channel, report):
    """Write a runner script that records the command's exit code and optionally
    posts a Discord line on finish. tmux runs `bash <script>` — putting the body
    in a file avoids all the quoting pitfalls of passing it as a tmux argument."""
    log = _log_path(name)
    done = os.path.join(STATE_DIR, name + ".exit")
    lines = [
        "#!/bin/bash",
        f"({command}) 2>&1 | tee {shlex.quote(log)}",
        f"echo ${{PIPESTATUS[0]}} > {shlex.quote(done)}",
    ]
    if report and channel:
        api = shlex.quote(os.path.join(ROOT, "discord_api.py"))
        # Build the message into a shell variable with the dynamic parts captured
        # FIRST, then post "$MSG" as a single argv. This keeps backticks / quotes /
        # $(...) that may appear in the log tail INERT — they're never re-parsed by
        # the shell. (The old form interpolated `{name}` and $(tail ...) straight
        # into a double-quoted argument, so a kebab-case name like `video-demo`
        # was command-substituted away and a `"` in the tail broke the post.)
        prefix = shlex.quote(f"🤖 sub-agent `{name}` finished (exit ")
        lines += [
            f"EXIT=$(cat {shlex.quote(done)} 2>/dev/null)",
            f"TAIL=$(tail -n 3 {shlex.quote(log)} 2>/dev/null | tr '\\n\\r\\t' '   ' | cut -c1-300)",
            f'MSG="$(printf \'%s%s). tail: %s\' {prefix} "$EXIT" "$TAIL")"',
            f'python {api} post {shlex.quote(str(channel))} "$MSG" >/dev/null 2>&1 || true',
        ]
    lines.append("sleep 2")  # keep the pane briefly so `logs` works right after exit
    path = os.path.join(STATE_DIR, name + ".sh")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def cmd_spawn(a):
    _ensure_dirs()
    name = a.name
    if _alive(name):
        print(f"error: sub-agent '{name}' already running (kill it first)", file=sys.stderr)
        return 1
    command = a.command
    # Reset any stale exit marker.
    try:
        os.remove(os.path.join(STATE_DIR, name + ".exit"))
    except OSError:
        pass
    runner = _write_runner(command, name, a.channel, a.report)
    cwd = a.cwd or ROOT
    r = _tmux("new-session", "-d", "-s", _sess(name), "-c", cwd, "bash", runner)
    if r.returncode != 0:
        print(f"error: tmux failed: {r.stdout}", file=sys.stderr)
        return 1
    _save_state(
        name, command=command, cwd=cwd, channel=a.channel, note=a.note,
        report=bool(a.report), started_at=int(time.time()), kind="shell",
    )
    print(f"spawned sub-agent '{name}' in tmux session {_sess(name)}")
    print(f"  logs: subagent.py logs {name}    list: subagent.py list")
    return 0


def cmd_claude(a):
    """Spawn a sub-agent that is itself a headless claude -p run."""
    bin_ = os.environ.get("CLAUDE_BIN", "claude")
    parts = [bin_, "-p", "--permission-mode",
             os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions")]
    if a.model:
        parts += ["--model", a.model]
    parts.append(a.prompt)
    a.command = " ".join(shlex.quote(p) for p in parts)
    rc = cmd_spawn(a)
    if rc == 0:
        _save_state(a.name, kind="claude", prompt=a.prompt)
    return rc


def _status(name):
    if _alive(name):
        return "running"
    exit_marker = os.path.join(STATE_DIR, name + ".exit")
    if os.path.exists(exit_marker):
        try:
            return "done(exit " + open(exit_marker).read().strip() + ")"
        except Exception:
            return "done"
    return "ended"


def cmd_list(a):
    _ensure_dirs()
    names = sorted(f[:-5] for f in os.listdir(STATE_DIR) if f.endswith(".json"))
    if not names:
        print("(no sub-agents tracked)")
        return 0
    for name in names:
        st = _load_state(name)
        age = ""
        if st.get("started_at"):
            age = f"{int((time.time() - st['started_at']) // 60)}m ago"
        line = f"{name:24} {_status(name):16} {age:10}"
        if st.get("note"):
            line += f"  — {st['note']}"
        print(line)
        if st.get("kind") == "claude" and st.get("prompt"):
            print(f"{'':24} claude: {st['prompt'][:80]}")
    return 0


def cmd_logs(a):
    # Prefer the on-disk log (full history); fall back to the live pane.
    log = _log_path(a.name)
    if os.path.exists(log):
        out = subprocess.run(["tail", "-n", str(a.lines), log],
                             text=True, stdout=subprocess.PIPE).stdout
        print(out, end="")
        return 0
    if _alive(a.name):
        r = _tmux("capture-pane", "-p", "-t", _sess(a.name))
        print("\n".join(r.stdout.splitlines()[-a.lines:]))
        return 0
    print(f"(no logs for '{a.name}')", file=sys.stderr)
    return 1


def cmd_send(a):
    if not _alive(a.name):
        print(f"error: '{a.name}' is not running", file=sys.stderr)
        return 1
    # Send the literal text followed by Enter so you can drive an interactive job.
    _tmux("send-keys", "-t", _sess(a.name), a.keys, "Enter")
    print(f"sent to '{a.name}': {a.keys}")
    return 0


def cmd_wait(a):
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        if not _alive(a.name):
            print(_status(a.name))
            return 0
        time.sleep(2)
    print(f"still running after {a.timeout}s (use `logs`/`list` later)")
    return 2


def cmd_kill(a):
    if _alive(a.name):
        _tmux("kill-session", "-t", _sess(a.name))
    _save_state(a.name, killed_at=int(time.time()))
    print(f"killed '{a.name}'")
    return 0


def cmd_reap(a):
    _ensure_dirs()
    reaped = []
    for f in os.listdir(STATE_DIR):
        if not f.endswith(".json"):
            continue
        name = f[:-5]
        if not _alive(name):
            for p in (_state_path(name), os.path.join(STATE_DIR, name + ".exit")):
                try:
                    os.remove(p)
                except OSError:
                    pass
            reaped.append(name)
    print("reaped: " + (", ".join(reaped) if reaped else "(none)"))
    return 0


def main():
    p = argparse.ArgumentParser(description="Sub-agent manager (tmux-backed).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("spawn", help="start a detached shell command")
    sp.add_argument("name")
    sp.add_argument("command", help="the shell command, as ONE quoted string")
    sp.add_argument("--cwd")
    sp.add_argument("--channel")
    sp.add_argument("--note", default="")
    sp.add_argument("--report", action="store_true")
    sp.set_defaults(func=cmd_spawn)

    sc = sub.add_parser("claude", help="spawn a headless claude -p sub-agent")
    sc.add_argument("name")
    sc.add_argument("prompt")
    sc.add_argument("--cwd")
    sc.add_argument("--channel")
    sc.add_argument("--note", default="")
    sc.add_argument("--model", default="")
    sc.add_argument("--report", action="store_true")
    sc.set_defaults(func=cmd_claude)

    sl = sub.add_parser("list", help="list tracked sub-agents + status")
    sl.set_defaults(func=cmd_list)

    sg = sub.add_parser("logs", help="show recent output")
    sg.add_argument("name")
    sg.add_argument("--lines", type=int, default=40)
    sg.set_defaults(func=cmd_logs)

    ss = sub.add_parser("send", help="type input into a running session")
    ss.add_argument("name")
    ss.add_argument("keys")
    ss.set_defaults(func=cmd_send)

    sw = sub.add_parser("wait", help="block until it exits")
    sw.add_argument("name")
    sw.add_argument("--timeout", type=int, default=60)
    sw.set_defaults(func=cmd_wait)

    sk = sub.add_parser("kill", help="stop + record")
    sk.add_argument("name")
    sk.set_defaults(func=cmd_kill)

    sr = sub.add_parser("reap", help="drop state for ended sessions")
    sr.set_defaults(func=cmd_reap)

    a = p.parse_args()
    sys.exit(a.func(a))


if __name__ == "__main__":
    main()
