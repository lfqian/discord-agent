#!/usr/bin/env python3
"""CowBot live monitor — a multi-panel terminal dashboard.

Run inside the container (reads /workspace telemetry only — no hooks into the
bot, so it can't break anything):

    container exec -it discord-agent python /app/src/monitor.py

Top panel: worker utilization (n/N working), sessions, rate-limit, today's cost.
Middle: one live panel per running agent showing its current phase + thinking /
tool-use. Bottom: recent completed runs. Ctrl-C to quit.
"""
import glob
import json
import os
import select
import subprocess
import sys
import termios
import time
import tty

import httpx
from dotenv import load_dotenv
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

WORKSPACE = os.environ.get("DISCORD_AGENT_WORKSPACE", "/workspace")
RUNS_DIR = os.path.join(WORKSPACE, ".runs")
# Read the real config the bridge/gateway use, not just the exec environment.
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
WORKERS = int(os.environ.get("GATEWAY_WORKERS", "4"))
SESSIONS_FILE = os.path.join(WORKSPACE, ".sessions.json")
SERVERS_DIR = os.path.join(WORKSPACE, "servers")
SUBAGENTS_DIR = os.path.join(WORKSPACE, "subagents")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
PHASE_STYLE = {"💭 thinking": "yellow", "✍️ replying": "green", "done": "dim", "starting": "cyan"}

# Resolve guild/channel ids → human names via the bot token, cached to disk so we
# don't hit the API on every refresh (names rarely change).
_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
_NAME_CACHE_FILE = os.path.join(WORKSPACE, ".namecache.json")
try:
    with open(_NAME_CACHE_FILE) as _f:
        _names = json.load(_f)
except Exception:
    _names = {}


def _api(path):
    try:
        r = httpx.get("https://discord.com/api/v10" + path,
                      headers={"Authorization": f"Bot {_TOKEN}"}, timeout=6)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _save_names():
    try:
        with open(_NAME_CACHE_FILE, "w") as f:
            json.dump(_names, f)
    except Exception:
        pass


def guild_name(gid):
    gid = str(gid or "")
    if not gid:
        return "?"
    k = "g:" + gid
    if k not in _names:
        _names[k] = (_api(f"/guilds/{gid}").get("name") or gid[:8])
        _save_names()
    return _names[k]


def channel_info(cid):
    """→ (channel_name, guild_id). Cached."""
    cid = str(cid or "")
    if not cid:
        return "?", ""
    k = "c:" + cid
    if k not in _names:
        d = _api(f"/channels/{cid}")
        _names[k] = {"name": d.get("name") or cid[:8], "guild": str(d.get("guild_id") or "")}
        _save_names()
    v = _names[k]
    return v["name"], v["guild"]


def _json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _runs():
    out = []
    for f in glob.glob(os.path.join(RUNS_DIR, "*.json")):
        d = _json(f, None)
        if not d:
            continue
        pid = d.get("pid")
        if pid and not _alive(pid):  # process gone — stale telemetry, clean up
            try:
                os.remove(f)
            except OSError:
                pass
            continue
        out.append(d)
    return sorted(out, key=lambda d: d.get("start", 0))


def _usage():
    rows = []
    try:
        with open(os.path.join(WORKSPACE, ".usage.jsonl")) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


def _dur(s):
    s = int(s)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _short(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def overview(runs):
    now = time.time()
    busy = len(runs)
    try:
        lim = float(open(os.path.join(WORKSPACE, ".limited_until")).read().strip())
    except Exception:
        lim = 0
    deferred = len(glob.glob(os.path.join(WORKSPACE, ".deferred", "*.json")))
    sessions = len(_json(os.path.join(WORKSPACE, ".sessions.json"), {}))
    watched = len(_json(os.path.join(WORKSPACE, ".bridge_cursors.json"), {}))
    u = _usage()
    today = [r for r in u if now - (r.get("ts") or 0) < 86400]
    spent = sum(r.get("cost_usd") or 0 for r in today)
    bar = "█" * busy + "░" * max(0, WORKERS - busy)
    barcol = "red" if busy >= WORKERS else "green"
    lim_s = f"[red]limited→{time.strftime('%H:%M UTC', time.gmtime(lim))}[/]" if lim > now else "[green]ok[/]"
    t = Table.grid(expand=True)
    t.add_column(ratio=1)
    t.add_column(justify="right", ratio=1)
    t.add_row(f"[bold cyan]workers[/] [{barcol}]{bar}[/] {busy}/{WORKERS} working",
              f"rate-limit: {lim_s}    deferred: {deferred}")
    t.add_row(f"sessions: {sessions}    watched: {watched} ch",
              f"today: {len(today)} runs    [bold]${spent:.2f}[/]")
    return Panel(t, title="🍡 CowBot", subtitle=time.strftime("%H:%M:%S"),
                 border_style="cyan", padding=(0, 1))


def agent_panel(d):
    now = time.time()
    phase = d.get("phase", "?")
    style = PHASE_STYLE.get(phase, "white")
    el = _dur(now - (d.get("start") or now))
    think = (d.get("thinking") or "").strip() or "…"
    head = Text.assemble(("● ", style), (phase, f"bold {style}"), ("   ", ""), (el, "dim"))
    cname, _gid = channel_info(d.get("channel"))
    meta = Text(f"{_short(guild_name(d.get('server')), 16)} · #{_short(cname, 18)} · "
                f"{_short(d.get('user', '?'), 14)}", style="dim cyan")
    body = Text(think[-360:], style="dim")
    return Panel(Group(head, meta, Text(""), body), border_style=style,
                 title=f"agent {d.get('pid', '?')}", width=56, padding=(0, 1))


def recent():
    u = _usage()
    t = Table(title="recent runs", expand=True, border_style="dim", title_style="bold dim")
    t.add_column("server / channel")
    t.add_column("cost", justify="right")
    t.add_column("turns", justify="right")
    t.add_column("dur", justify="right")
    for r in list(reversed(u))[:6]:
        cname, gid = channel_info(r.get("channel"))
        loc = f"{_short(guild_name(gid), 12)}/#{_short(cname, 16)}"
        t.add_row(loc, f"${(r.get('cost_usd') or 0):.4f}",
                  str(r.get("turns") or "?"), f"{(r.get('duration_ms') or 0) // 1000}s")
    return t


def _tmux_alive(name):
    try:
        return subprocess.run(["tmux", "has-session", "-t", f"sa-{name}"],
                              capture_output=True).returncode == 0
    except Exception:
        return False


def subagents():
    """Background tmux sub-agents tracked under /workspace/subagents/: running
    (tmux session alive), done (exit marker), or stopped. Running first."""
    out = []
    for f in glob.glob(os.path.join(SUBAGENTS_DIR, "*.json")):
        d = _json(f, None)
        if not d:
            continue
        name = d.get("name") or os.path.basename(f)[:-5]
        exit_f = os.path.join(SUBAGENTS_DIR, name + ".exit")
        if os.path.exists(exit_f):
            try:
                code = open(exit_f).read().strip()
            except Exception:
                code = "?"
            status, running = f"done · exit {code}", False
        elif _tmux_alive(name):
            status, running = "running", True
        else:
            status, running = "stopped", False
        out.append({"name": name, "status": status, "running": running,
                    "channel": d.get("channel"), "started": d.get("started_at"),
                    "note": d.get("note") or "", "report": d.get("report")})
    # Keep all running; drop finished/stopped ones older than a day so the panel
    # stays focused on what's actually current.
    now = time.time()
    out = [s for s in out if s["running"] or (now - (s.get("started") or 0) < 86400)]
    out.sort(key=lambda s: (not s["running"], -(s.get("started") or 0)))
    return out


def subagents_panel(subs):
    t = Table.grid(padding=(0, 1))
    for _ in range(4):
        t.add_column()
    for s in subs[:7]:
        el = _dur(time.time() - s["started"]) if s.get("started") else "?"
        cname = channel_info(s["channel"])[0] if s.get("channel") else "—"
        if s["running"]:
            dot = "[green]●[/]"
        elif s["status"].endswith("exit 0"):
            dot = "[dim]●[/]"
        else:
            dot = "[red]●[/]"
        rep = " [dim]→ch[/]" if s.get("report") else ""
        t.add_row(dot, f"[bold]{_short(s['name'], 16)}[/]",
                  f"[dim]{s['status']}[/]   [dim]#{_short(cname, 14)} · {el}{rep}[/]")
    nrun = sum(1 for s in subs if s["running"])
    return Panel(t, title=f"sub-agents (background) — {nrun} running",
                 border_style="blue", padding=(0, 1))


def resumable():
    """Resumable per-channel sessions (skip the internal sweep sessions)."""
    d = _json(SESSIONS_FILE, {})
    return [{"channel": ch, "sid": sid} for ch, sid in d.items() if not ch.startswith("__")][:9]


def sessions_panel(sess):
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bold magenta", justify="right")
    t.add_column()
    for i, s in enumerate(sess, 1):
        cname, gid = channel_info(s["channel"])
        t.add_row(f"{i}", f"{_short(guild_name(gid), 14)} · #{_short(cname, 18)}  "
                          f"[dim]{s['sid'][:8]}[/]")
    if not sess:
        t.add_row("", "[dim](no sessions yet)[/]")
    return Panel(t, title="resume & chat — press [1-9]   ·   q quit",
                 border_style="magenta", padding=(0, 1))


def _channel_busy(channel):
    for f in glob.glob(os.path.join(RUNS_DIR, "*.json")):
        d = _json(f, {})
        if str(d.get("channel")) == str(channel) and d.get("pid") and _alive(d.get("pid")):
            return True
    return False


def resume_session(entry):
    """Drop into an interactive `claude --resume` for this channel's session, in
    the right server's config dir/cwd. Returns when the operator exits claude."""
    ch, sid = entry["channel"], entry["sid"]
    cname, gid = channel_info(ch)
    if not gid:
        print(f"\n⚠️  couldn't resolve a server for #{cname}."); time.sleep(2); return
    if _channel_busy(ch):
        print(f"\n⚠️  #{cname} is busy (an agent is running) — try again when idle."); time.sleep(2); return
    sd = os.path.join(SERVERS_DIR, gid, "channels", str(ch))
    os.makedirs(os.path.join(sd, ".claude"), exist_ok=True)
    os.makedirs(os.path.join(sd, "tmp"), exist_ok=True)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(sd, ".claude"),
               AGENT_CURRENT_GUILD=gid, AGENT_SERVER_DIR=sd, TMPDIR=os.path.join(sd, "tmp"))
    cmd = [CLAUDE_BIN, "--resume", sid]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    print(f"\n── resuming #{cname} · {guild_name(gid)} — exit claude (/exit or Ctrl-D) to return ──\n", flush=True)
    try:
        subprocess.run(cmd, cwd=sd, env=env)
    except Exception as exc:
        print(f"resume failed: {exc}"); time.sleep(2)


def dashboard(sess=None):
    runs = _runs()
    if runs:
        mid = Columns([agent_panel(d) for d in runs], expand=True)
    else:
        mid = Panel(Text("idle — no agents running right now", style="dim italic"),
                    border_style="dim", padding=(0, 1))
    parts = [overview(runs), mid]
    subs = subagents()
    if subs:
        parts.append(subagents_panel(subs))
    if sess is not None:
        parts.append(sessions_panel(sess))
    parts.append(recent())
    return Group(*parts)


def _poll_key(timeout):
    end = time.time() + timeout
    while time.time() < end:
        if select.select([sys.stdin], [], [], 0.05)[0]:
            try:
                return sys.stdin.read(1)
            except Exception:
                return None
    return None


def main():
    istty = sys.stdin.isatty()
    old = None
    if istty:
        try:
            old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except Exception:
            old = None
    try:
        with Live(console=Console(), screen=True, refresh_per_second=4) as live:
            while True:
                sess = resumable()
                live.update(dashboard(sess))
                if istty:
                    key = _poll_key(0.5)
                else:
                    time.sleep(0.5)
                    key = None
                if key in ("q", "Q", "\x03"):
                    break
                if key and key.isdigit() and key != "0":
                    idx = int(key) - 1
                    if idx < len(sess):
                        live.stop()
                        if old:
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
                        resume_session(sess[idx])
                        if old:
                            tty.setcbreak(sys.stdin.fileno())
                        live.start()
    except KeyboardInterrupt:
        pass
    finally:
        if old:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)


if __name__ == "__main__":
    main()
