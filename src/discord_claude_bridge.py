"""Bridge Discord mentions to the Claude Code CLI.

When DISCORD_GUILD_ID is set, the bridge watches every text channel in that
guild that the bot can read, so it can be @-mentioned from anywhere and replies
go back to the channel the message came from. Without a guild id it falls back
to the single DISCORD_CHANNEL_ID (legacy behaviour).

In the container setup CLAUDE_CWD points at the mounted source tree (/app), so
the agent can edit its own code when a session asks it to; edits land on the
host bind mount.
"""
import datetime
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import httpx
from dotenv import load_dotenv


ROOT = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.environ.get(
    "DISCORD_AGENT_WORKSPACE",
    os.path.expanduser("~/discordAgentWorkspace"),
)
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(WORKSPACE, ".env"), override=True)
# Pipeline / task credentials (DSN, AWS, LLM keys, GH_TOKEN, …) live in a
# separate secrets file so they're not mixed with bot config. Loaded into the
# environment that the spawned `claude` (and its shell commands) inherit.
load_dotenv(os.path.join(WORKSPACE, "secrets.env"), override=True)

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
# Optional restriction: blank → operate in EVERY guild the bot has joined;
# one id or a comma-separated list → only those guilds.
GUILD_IDS = [g.strip() for g in os.environ.get("DISCORD_GUILD_ID", "").split(",") if g.strip()]
BOT_ID = os.environ["DISCORD_BOT_ID"]
ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_CWD = os.environ.get("CLAUDE_CWD", ROOT)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip()
# Reasoning effort: low | medium | high | xhigh | max. Higher = more thinking
# (better on hard tasks, but slower and burns more quota). Blank = claude default.
CLAUDE_EFFORT = os.environ.get("CLAUDE_EFFORT", "").strip()
# Live telemetry: stream claude's output (stream-json) so the monitor can show
# each agent's thinking/tool-use in real time. Set STREAM_TELEMETRY=0 to fall back
# to the plain one-shot json path (instant escape hatch if streaming misbehaves).
STREAM_TELEMETRY = os.environ.get("STREAM_TELEMETRY", "1").strip().lower() not in ("0", "", "false", "no")
RUNS_DIR = os.path.join(WORKSPACE, ".runs")
# Permission mode for the in-container claude. "dontAsk" blocks ALL shell
# execution (claude can only read/write files); that's why the bot couldn't
# post, run pipelines, etc. "bypassPermissions" lets it actually act — safe here
# because the container is the isolation boundary.
PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "bypassPermissions").strip()
TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "10"))
MAX_RESPONSE_CHARS = int(os.environ.get("MAX_RESPONSE_CHARS", "1800"))
HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "20"))
# Tiered context (opt-in via COMPACT_HISTORY=1). When on, fetch a deeper window,
# keep the last HISTORY_VERBATIM messages verbatim, and fold everything older
# into a running summary produced by a small/cheap model (COMPACT_MODEL). The
# summary is cached per-channel and updated incrementally (only newly aged-out
# messages get summarized each turn), so we don't re-compress the whole backlog.
# Any failure degrades gracefully back to the flat HISTORY_LIMIT behavior.
COMPACT_HISTORY = os.environ.get("COMPACT_HISTORY", "0").strip().lower() not in ("0", "", "false", "no")
HISTORY_VERBATIM = int(os.environ.get("HISTORY_VERBATIM", "8"))   # newest msgs kept raw
HISTORY_DEEP = int(os.environ.get("HISTORY_DEEP", "120"))         # max msgs ever pulled
COMPACT_MODEL = os.environ.get("COMPACT_MODEL", "haiku").strip()
COMPACT_TIMEOUT = int(os.environ.get("COMPACT_TIMEOUT_SECONDS", "60"))
COMPACT_MAX_WORDS = int(os.environ.get("COMPACT_MAX_WORDS", "180"))
HISTORY_COMPACT_DIR = os.path.join(WORKSPACE, ".history_compact")
# Drop-from-context marking (opt-in via DROP_MARKED_FROM_CONTEXT=1). When on, any
# message someone reacts to with DROP_MARK_EMOJI (default ❌) is removed from the
# context window the model sees; if the marked message is a user message, the
# contiguous run of the bot's own replies right after it is dropped too — so
# marking a Q&A with ❌ makes that whole exchange stop participating in future
# context. It does NOT delete anything on Discord. Caveat: a message already
# folded into the tiered rolling summary can't be retroactively un-summarized;
# this reliably hides messages still in the verbatim/unsummarized range (the
# common case: you mark something you just saw). Degrades to no-op on any error.
DROP_MARKED = os.environ.get("DROP_MARKED_FROM_CONTEXT", "0").strip().lower() not in ("0", "", "false", "no")
DROP_MARK_EMOJI = os.environ.get("DROP_MARK_EMOJI", "❌").strip()
# DELETE marked messages on Discord — a DISTINCT, heavier action from drop. The
# delete mark is a SEPARATE emoji (DELETE_MARK_EMOJI, default 🗑️) so ❌ stays safe
# ("just ignore from context, reversible") and only 🗑️ actually removes the
# message. A bare reaction doesn't wake the bot, so deletion runs as an
# activity-gated sweep on the tick loop (see sweep_marked_deletions): it deletes
# anything bearing DELETE_MARK_EMOJI plus the bot's reply run right after a marked
# user message. Deleting the bot's own messages always works; deleting someone
# else's needs the "Manage Messages" permission — without it those just stay
# (still hidden from context). Opt-in, global single-process switch. Best-effort.
DELETE_MARKED = os.environ.get("DELETE_MARKED_MESSAGES", "0").strip().lower() not in ("0", "", "false", "no")
DELETE_MARK_EMOJI = os.environ.get("DELETE_MARK_EMOJI", "🗑️").strip()
DELETE_SWEEP_LIMIT = int(os.environ.get("DELETE_SWEEP_LIMIT", "50"))  # msgs scanned/sweep/channel
# Marked-deletion sweep is activity-gated: it runs for a channel when that channel
# had new messages this tick, plus a coarse fallback so a 🗑️ on a quiet channel
# still gets caught within this interval. Avoids scanning every channel every tick.
MARK_SWEEP_FALLBACK = int(os.environ.get("MARK_SWEEP_FALLBACK_SECONDS", "300"))
_DELETE_SKIP = set()       # message ids we already tried and couldn't delete (e.g. no perm)
_LAST_MARK_SWEEP = {}      # channel_id -> last sweep epoch
# Single activity signal shared with the runtime: bumped whenever a human posts.
# Both the marked-deletion sweep (here) and the proactive review (runtime) key off
# real activity instead of each keeping its own blind timer.
ACTIVITY_FILE = os.path.join(WORKSPACE, ".activity")
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "240"))
# Per-channel "last seen message id" cursors, so each channel is tracked
# independently. Replaces the old single-channel .claude_bridge_seen file.
CURSORS_FILE = os.path.join(WORKSPACE, ".bridge_cursors.json")
# Channel whitelist: when non-empty, the bot ONLY responds to @mentions in these
# channels. Empty (default) = respond everywhere (existing behaviour). Bot admins
# manage this at runtime via /whitelist commands; changes take effect immediately.
CHANNEL_WHITELIST_FILE = os.path.join(WORKSPACE, ".channel_whitelist.json")
# Channel links: data-driven cross-channel connection (edit the JSON, no code or
# prompt changes). Maps a channel id -> a target working context so the channel
# SHARES that context's dir (memory + session), and optionally pulls recent
# messages from `watch` channels into its context. Format:
#   { "<channel_id>": {"guild": "<gid>", "channel": "<cid or omit for guild-level>",
#                       "watch": ["<cid>", ...]} }
CHANNEL_LINKS_FILE = os.path.join(WORKSPACE, ".channel_links.json")


def _channel_links():
    """Load the channel-link map; {} if missing/invalid (feature off by default)."""
    try:
        with open(CHANNEL_LINKS_FILE) as f:
            return json.load(f) or {}
    except Exception:
        return {}
# Per-server isolated working dirs: each guild gets its own scratch/clones,
# session store (CLAUDE_CONFIG_DIR) and TMPDIR under here, so files written while
# serving one server are not in another server's working directory.
SERVERS_DIR = os.path.join(WORKSPACE, "servers")
# Atomic per-message claim dir, so the REST poller and the Gateway event path
# never both handle (and reply to) the same message.
HANDLED_DIR = os.path.join(WORKSPACE, ".handled")
# Per-channel Claude session ids. Each channel/thread becomes its own ongoing
# conversation that survives across invocations via `claude --resume`, so the
# bot keeps context for continuous dev work instead of starting cold each time.
# Transcripts live under CLAUDE_CONFIG_DIR (point it at /workspace to persist
# across container rebuilds). Send "/reset" in a channel to start fresh there.
SESSIONS_FILE = os.path.join(WORKSPACE, ".sessions.json")
SESSION_RESUME = os.environ.get("SESSION_RESUME", "1").strip().lower() not in ("0", "", "false", "no")
# Per-channel context watermark: the id of the last message the bot's session has
# seen. Lets us inject ONLY new messages since the bot's last turn (the gap the
# session doesn't have) instead of re-feeding a fixed window every time.
CONTEXT_CURSORS_FILE = os.path.join(WORKSPACE, ".context_cursors.json")
_ctx_lock = threading.Lock()
# Discord channel types we treat as text we can be summoned in.
TEXT_CHANNEL_TYPES = {0, 5, 10, 11, 12}
# Tier-1: reply chunking, attachments, usage tracking.
USAGE_FILE = os.path.join(WORKSPACE, ".usage.jsonl")
REPLY_CHUNK = int(os.environ.get("REPLY_CHUNK", "1900"))            # chars per posted msg
REPLY_FILE_THRESHOLD = int(os.environ.get("REPLY_FILE_THRESHOLD", "6000"))  # above → file
ATTACH_MAX_BYTES = int(os.environ.get("ATTACH_MAX_BYTES", str(25 * 1024 * 1024)))
START_TIME = time.time()
# Rate-limit gate: when Claude reports a usage/session limit, parse the reset
# time, pause until then, and queue requests so they're auto-answered on resume.
LIMITED_FILE = os.path.join(WORKSPACE, ".limited_until")
DEFERRED_DIR = os.path.join(WORKSPACE, ".deferred")
LIMIT_DEFAULT_COOLDOWN = int(os.environ.get("LIMIT_DEFAULT_COOLDOWN", "3600"))
HELP_TEXT = (
    "**Lingfei 的专属 bot** — @ 我即可。我能:\n"
    "• 读/写本服务器任意频道和 forum thread\n"
    "• 跑命令、读改自己的代码(热重载)、`ssh fin-agent`、查 DB/pipeline\n"
    "• 看你贴的图片/文件(截图报错也行)\n"
    "• 每个频道是一段持续会话(记得上下文);长任务会分阶段汇报\n"
    "• 每小时静默巡检,有需要才出声\n"
    "指令:`/help` · `/status` · `/reset`(重开本频道会话)"
)

allowed_raw = os.environ.get("BOT_ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {x.strip() for x in allowed_raw.split(",") if x.strip()}

# Anti-loop for bot↔bot @-mention ping-pong. Discord marks bot/webhook authors
# with author.bot == true, so we can tell them apart from humans.
#
# Whether to keep replying to a bot is judged by PROGRESS, not by a round count —
# a long genuine task may need many back-and-forth turns and must NOT be cut off
# just for being long. Two independent gates decide:
#   (a) Content classifier (cheap model): is the latest bot message real,
#       actionable work, or idle chatter / no-progress filler? Chatter → react
#       only. This naturally allows arbitrarily long collaboration AS LONG AS work
#       is happening, and goes quiet the moment it degrades into pleasantries.
#   (b) Stall/repetition detector: the real "stuck loop" signature is bots
#       echoing near-identical messages. If the last BOT_STALL_MAX bot turns are
#       near-duplicates (ratio ≥ BOT_REPEAT_RATIO) we break; ANY novel/progressing
#       message resets the stall counter. So varied, advancing work never trips it.
# BOT_REPLY_MAX is ONLY a catastrophic token-burn ceiling (set high; real
# collaboration shouldn't reach it). A human message resets all per-channel state.
#   REPLY_TO_BOTS=0  → never reply to bots (react-only).
#   BOT_JUDGE=0      → skip the content classifier (stall detector still applies).
REPLY_TO_BOTS = os.environ.get("REPLY_TO_BOTS", "1").strip().lower() not in ("0", "", "false", "no")
BOT_JUDGE = os.environ.get("BOT_JUDGE", "1").strip().lower() not in ("0", "", "false", "no")
BOT_STALL_MAX = int(os.environ.get("BOT_STALL_MAX", "4"))      # consecutive repeats → stop
BOT_REPEAT_RATIO = float(os.environ.get("BOT_REPEAT_RATIO", "0.985"))  # near-verbatim only
BOT_REPLY_MAX = int(os.environ.get("BOT_REPLY_MAX", "40"))     # catastrophic ceiling only
BOT_ACK_EMOJI = os.environ.get("BOT_ACK_EMOJI", "🤖")
_bot_reply_streak = {}   # channel_id -> consecutive replies to bot authors (ceiling)
_bot_recent_msgs = {}    # channel_id -> recent normalized bot message texts
_bot_stall = {}          # channel_id -> consecutive near-duplicate bot turns

HEADERS = {"Authorization": f"Bot {TOKEN}"}
POST_HEADERS = {**HEADERS, "Content-Type": "application/json"}
WS_RE = re.compile(r"\s+")

# Channels we got 403 on — skip them on later ticks instead of retrying forever.
DENIED = set()
# channel_id → guild_id, so the REST poller (whose message objects lack guild_id)
# can still scope handling to the right server. Populated by list_text_channels.
CHANNEL_GUILD = {}

# Session map writes + per-channel serialization (so concurrent messages in the
# same channel don't resume the same session at once and clobber it).
_sessions_lock = threading.Lock()
_channel_locks = {}
_channel_locks_guard = threading.Lock()

# Hot-reload: watch our own source + the .env files. When any changes, re-exec
# this process in place (same PID) so edits take effect without a container
# restart. The Claude side is already hot (a fresh `claude -p` per message reads
# files live); this covers the long-running bridge itself.
SELF = os.path.abspath(__file__)
WATCH_FILES = [
    SELF,
    os.path.join(ROOT, ".env"),
    os.path.join(WORKSPACE, ".env"),
    os.path.join(WORKSPACE, "secrets.env"),
]


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


WATCH_MTIMES = {p: _mtime(p) for p in WATCH_FILES}
_reload_warned = None


def maybe_reload():
    """If our source or .env changed, validate and re-exec in place."""
    global _reload_warned
    changed = [p for p in WATCH_FILES if _mtime(p) > WATCH_MTIMES.get(p, 0.0)]
    if not changed:
        return
    # Never swap in code that won't even import — a broken edit would crash the
    # bridge and (with no restart policy) take the whole container down.
    try:
        with open(SELF) as f:
            compile(f.read(), SELF, "exec")
    except SyntaxError as exc:
        sig = (_mtime(SELF), str(exc))
        if sig != _reload_warned:
            print(f"[bridge] reload skipped — syntax error: {exc}", flush=True)
            _reload_warned = sig
        return
    names = ", ".join(os.path.basename(p) for p in changed)
    print(f"[bridge] change in {names} — hot-reloading (execv)", flush=True)
    os.execv(sys.executable, [sys.executable, SELF])


def is_addressed(content):
    if f"<@{BOT_ID}>" in content or f"<@!{BOT_ID}>" in content:
        return True
    return bool(ROLE_ID and f"<@&{ROLE_ID}>" in content)


def clean_prompt(content):
    content = content.replace(f"<@{BOT_ID}>", "").replace(f"<@!{BOT_ID}>", "")
    if ROLE_ID:
        content = content.replace(f"<@&{ROLE_ID}>", "")
    return WS_RE.sub(" ", content).strip()


def load_cursors():
    if os.path.exists(CURSORS_FILE):
        try:
            with open(CURSORS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
    return {}


def save_cursors(cursors):
    os.makedirs(WORKSPACE, exist_ok=True)
    tmp = CURSORS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cursors, f)
    os.replace(tmp, CURSORS_FILE)


def load_channel_whitelist():
    """Returns the set of whitelisted channel ids, or empty set (= no restriction)."""
    try:
        with open(CHANNEL_WHITELIST_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(x) for x in data if x}
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[bridge] whitelist read error: {exc}", flush=True)
    return set()


def save_channel_whitelist(ids):
    tmp = CHANNEL_WHITELIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(ids), f)
    os.replace(tmp, CHANNEL_WHITELIST_FILE)


_guilds_cache = {"ids": None, "ts": 0.0}
GUILD_CACHE_TTL = int(os.environ.get("GUILD_CACHE_TTL", "300"))


def list_guilds():
    """Guild ids to operate in. If DISCORD_GUILD_ID is set (one or comma-separated)
    use those; otherwise every guild the bot has joined (cached, since membership
    rarely changes and we don't want to hit /users/@me/guilds every tick)."""
    if GUILD_IDS:
        return GUILD_IDS
    now = time.time()
    if _guilds_cache["ids"] is not None and now - _guilds_cache["ts"] < GUILD_CACHE_TTL:
        return _guilds_cache["ids"]
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get("https://discord.com/api/v10/users/@me/guilds")
        response.raise_for_status()
        data = response.json()
    ids = [str(g["id"]) for g in data if isinstance(g, dict) and g.get("id")]
    _guilds_cache["ids"] = ids
    _guilds_cache["ts"] = now
    return ids


def list_active_threads(guild_id):
    """Active thread ids in a guild (forum posts, threads under text channels).
    `GET /guilds/{id}/channels` does NOT return threads, so without this a @ in a
    forum channel like omega is never seen. A thread is just a channel for the
    messages/post endpoints, so its id slots straight into the watch list."""
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/guilds/{guild_id}/threads/active",
        )
        response.raise_for_status()
        data = response.json()
    threads = data.get("threads", []) if isinstance(data, dict) else []
    return [str(t["id"]) for t in threads if isinstance(t, dict) and t.get("id")]


def list_text_channels():
    """Channel ids to watch this tick: every text channel + active thread across
    all guilds the bot operates in (forum posts included). Falls back to the
    single configured channel if no guilds are reachable."""
    try:
        guilds = list_guilds()
    except Exception as exc:
        print(f"[bridge] guild list error: {exc}", flush=True)
        guilds = []
    ids = []
    for guild_id in guilds:
        try:
            with httpx.Client(timeout=20, headers=HEADERS) as client:
                response = client.get(
                    f"https://discord.com/api/v10/guilds/{guild_id}/channels",
                )
                response.raise_for_status()
                channels = response.json()
            for c in channels:
                if isinstance(c, dict) and c.get("type") in TEXT_CHANNEL_TYPES:
                    ids.append(str(c["id"]))
                    CHANNEL_GUILD[str(c["id"])] = str(guild_id)
        except Exception as exc:
            print(f"[bridge] channel list error ({guild_id}): {exc}", flush=True)
            continue
        try:
            for tid in list_active_threads(guild_id):
                CHANNEL_GUILD[tid] = str(guild_id)
                if tid not in ids:
                    ids.append(tid)
        except Exception as exc:
            print(f"[bridge] active-thread list error ({guild_id}): {exc}", flush=True)
    if CHANNEL_ID and CHANNEL_ID not in ids:
        ids.append(CHANNEL_ID)
    if not ids and CHANNEL_ID:
        ids = [CHANNEL_ID]
    return [c for c in ids if c not in DENIED]


def claim_message(message_id):
    """Atomically claim a message id. Returns True only for the first caller, so
    whichever of the poller / gateway gets there first handles it; the other
    skips. The gateway is near-instant, so it almost always wins."""
    os.makedirs(HANDLED_DIR, exist_ok=True)
    path = os.path.join(HANDLED_DIR, str(message_id))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def prune_handled(max_age=86400):
    try:
        now = time.time()
        for name in os.listdir(HANDLED_DIR):
            p = os.path.join(HANDLED_DIR, name)
            try:
                if now - os.path.getmtime(p) > max_age:
                    os.remove(p)
            except OSError:
                pass
    except FileNotFoundError:
        pass


def _read_sessions():
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def get_session(channel_id):
    return _read_sessions().get(str(channel_id))


def set_session(channel_id, session_id):
    with _sessions_lock:
        data = _read_sessions()
        data[str(channel_id)] = session_id
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, SESSIONS_FILE)


def clear_session(channel_id):
    with _sessions_lock:
        data = _read_sessions()
        if data.pop(str(channel_id), None) is not None:
            tmp = SESSIONS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, SESSIONS_FILE)


def _read_ctx():
    try:
        with open(CONTEXT_CURSORS_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def get_context_cursor(channel_id):
    return _read_ctx().get(str(channel_id))


def set_context_cursor(channel_id, message_id):
    with _ctx_lock:
        d = _read_ctx()
        d[str(channel_id)] = str(message_id)
        tmp = CONTEXT_CURSORS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, CONTEXT_CURSORS_FILE)


def clear_context_cursor(channel_id):
    with _ctx_lock:
        d = _read_ctx()
        if d.pop(str(channel_id), None) is not None:
            tmp = CONTEXT_CURSORS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, CONTEXT_CURSORS_FILE)


def channel_lock(channel_id):
    with _channel_locks_guard:
        lk = _channel_locks.get(str(channel_id))
        if lk is None:
            lk = threading.Lock()
            _channel_locks[str(channel_id)] = lk
        return lk


def fetch_messages(channel_id, after):
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": 50, "after": after},
        )
    if response.status_code == 403:
        DENIED.add(channel_id)
        return []
    response.raise_for_status()
    messages = response.json()
    if not isinstance(messages, list):
        return []
    return sorted(messages, key=lambda msg: int(msg["id"]))


def _msg_line(msg):
    name = (msg.get("author") or {}).get("username", "?")
    body = WS_RE.sub(" ", msg.get("content", "") or "").strip()
    return f"[{name}] {body}" if body else ""


def _fetch_before(channel_id, before_id, want):
    """Fetch up to `want` messages strictly before `before_id`, oldest-first,
    paginating in pages of 100 (Discord's per-request cap)."""
    out = []
    cursor = before_id
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        while len(out) < want:
            page = min(100, want - len(out))
            response = client.get(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                params={"limit": page, "before": cursor},
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)                       # API returns newest-first
            cursor = min(batch, key=lambda m: int(m["id"]))["id"]
            if len(batch) < page:
                break
    return sorted(out, key=lambda m: int(m["id"]))  # oldest-first


def _fetch_after(channel_id, after_id, want):
    """Fetch up to `want` messages strictly AFTER `after_id`, oldest-first."""
    out = []
    cursor = after_id
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        while len(out) < want:
            page = min(100, want - len(out))
            response = client.get(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                params={"limit": page, "after": cursor},
            )
            response.raise_for_status()
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            cursor = max(batch, key=lambda m: int(m["id"]))["id"]
            if len(batch) < page:
                break
    return sorted(out, key=lambda m: int(m["id"]))


def _reacted_with(msg, emoji):
    for r in (msg.get("reactions") or []):
        if (r.get("emoji") or {}).get("name") == emoji:
            return True
    return False


def _has_drop_mark(msg):
    # Either mark hides from context; 🗑️ additionally gets the message deleted.
    return _reacted_with(msg, DROP_MARK_EMOJI) or _reacted_with(msg, DELETE_MARK_EMOJI)


def _has_delete_mark(msg):
    return _reacted_with(msg, DELETE_MARK_EMOJI)


def _drop_marked(msgs):
    """Messages oldest-first in, filtered out. Removes any message reacted with
    DROP_MARK_EMOJI or DELETE_MARK_EMOJI; when a marked message is from a user,
    also removes the contiguous run of bot replies immediately after it. No-op
    unless DROP_MARKED is on. Never raises — returns the input unchanged on error."""
    if not DROP_MARKED or not msgs:
        return msgs
    try:
        drop = [False] * len(msgs)
        for i, m in enumerate(msgs):
            if _has_drop_mark(m):
                drop[i] = True
                if (m.get("author") or {}).get("id") != BOT_ID:
                    j = i + 1
                    while j < len(msgs) and (msgs[j].get("author") or {}).get("id") == BOT_ID:
                        drop[j] = True
                        j += 1
        return [m for m, d in zip(msgs, drop) if not d]
    except Exception:
        return msgs


def _fetch_recent_flat(channel_id, before_id):
    """Original behavior: last HISTORY_LIMIT messages as plain lines."""
    if HISTORY_LIMIT <= 0:
        return ""
    # When dropping marked messages, over-fetch so the window still fills up.
    want = HISTORY_LIMIT * 3 if DROP_MARKED else HISTORY_LIMIT
    msgs = _drop_marked(_fetch_before(channel_id, before_id, want))[-HISTORY_LIMIT:]
    return "\n".join(filter(None, (_msg_line(m) for m in msgs)))


def _compact_cache_path(channel_id):
    return os.path.join(HISTORY_COMPACT_DIR, f"{channel_id}.json")


def _load_compact_cache(channel_id):
    try:
        with open(_compact_cache_path(channel_id)) as f:
            data = json.load(f)
        return str(data.get("up_to") or "0"), str(data.get("summary") or "")
    except Exception:
        return "0", ""


def _save_compact_cache(channel_id, up_to, summary):
    try:
        os.makedirs(HISTORY_COMPACT_DIR, exist_ok=True)
        path = _compact_cache_path(channel_id)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump({"up_to": str(up_to), "summary": summary}, f)
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[bridge] compact cache write failed for {channel_id}: {exc}", flush=True)


def _small_model_summary(prompt):
    """One-shot summarization via a cheap model, tools disabled. Returns the
    text, or "" on any failure so callers can degrade gracefully."""
    cmd = [
        CLAUDE_BIN, "-p",
        "--permission-mode", "dontAsk",   # read-only: no shell, just summarize
        "--output-format", "json",
        "--model", COMPACT_MODEL,
        prompt,
    ]
    try:
        result = subprocess.run(
            cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=COMPACT_TIMEOUT, env=dict(os.environ),
        )
        if result.returncode != 0:
            return ""
        out = result.stdout.strip()
        try:
            return (json.loads(out).get("result") or "").strip()
        except Exception:
            return out
    except Exception as exc:
        print(f"[bridge] compact summarize failed: {exc}", flush=True)
        return ""


def _bot_should_engage(latest_text, history=""):
    """Content-based gate for bot↔bot messages: should we actually reply, or is
    this idle chatter / a no-progress loop we should let die (react only)?

    Uses the cheap model, tools disabled. Returns True (engage) / False (skip).
    Fails OPEN (True) on any error — the BOT_REPLY_MAX safety cap still bounds a
    runaway, so a flaky classifier never silently swallows real work."""
    snippet = (latest_text or "").strip()[:1500]
    ctx = (history or "")[-2000:]
    prompt = (
        "You gate a Discord bot's replies to messages from ANOTHER BOT, to stop "
        "two bots from @-pinging each other forever. Judge ONLY the latest bot "
        "message (with the recent context) and decide:\n"
        "- ENGAGE: it asks for or advances REAL work — a concrete request, data, "
        "a question that needs answering, a task hand-off, an error to fix, etc.\n"
        "- SKIP: it's filler with no actionable content — thanks/acknowledgements, "
        "pleasantries, 'ok'/'收到', restating what's done, or an echo that adds "
        "nothing and would just keep the loop going.\n"
        "When the exchange has clearly stopped making progress, choose SKIP.\n\n"
        f"--- recent context ---\n{ctx}\n\n"
        f"--- latest bot message ---\n{snippet}\n\n"
        "Answer with EXACTLY one word: ENGAGE or SKIP."
    )
    verdict = _small_model_summary(prompt)
    if not verdict:
        return True  # fail open; safety cap bounds the worst case
    return "SKIP" not in verdict.strip().upper()


def _compact_summary(channel_id, older):
    """Incremental running summary of the `older` messages (oldest-first).
    Only messages newer than what the cache already covers get folded in."""
    if not older:
        return ""
    newest_id = older[-1]["id"]
    up_to, summary = _load_compact_cache(channel_id)
    if summary and up_to == str(newest_id):
        return summary                              # nothing aged in since last turn
    fresh = [m for m in older if int(m["id"]) > int(up_to)] if up_to != "0" else older
    fresh_lines = "\n".join(filter(None, (_msg_line(m) for m in fresh)))
    if not fresh_lines:
        return summary
    if summary and len(fresh) < len(older):
        prompt = (
            "You maintain a rolling summary of an ongoing Discord conversation, "
            f"for an assistant's context. Keep it under {COMPACT_MAX_WORDS} words, "
            "preserving who asked what, decisions/answers reached, and any open "
            "tasks; drop small talk.\n\n"
            f"--- Existing summary ---\n{summary}\n\n"
            f"--- New messages to fold in ---\n{fresh_lines}\n\n"
            "Return ONLY the updated summary."
        )
    else:
        prompt = (
            "Summarize this Discord conversation for an assistant's context. "
            f"Keep it under {COMPACT_MAX_WORDS} words, preserving who asked what, "
            "decisions/answers reached, and any open tasks; drop small talk.\n\n"
            f"{fresh_lines}\n\nReturn ONLY the summary."
        )
    new_summary = _small_model_summary(prompt)
    if not new_summary:
        return summary                              # degrade: keep what we had
    _save_compact_cache(channel_id, newest_id, new_summary)
    return new_summary


def _fetch_recent_tiered(channel_id, before_id):
    """Deep window split into a summarized head + verbatim tail."""
    msgs = _drop_marked(_fetch_before(channel_id, before_id, HISTORY_DEEP))
    if not msgs:
        return ""
    tail = msgs[-HISTORY_VERBATIM:] if HISTORY_VERBATIM > 0 else msgs
    older = msgs[: -HISTORY_VERBATIM] if HISTORY_VERBATIM > 0 else []
    verbatim = "\n".join(filter(None, (_msg_line(m) for m in tail)))
    summary = _compact_summary(channel_id, older)
    if not summary:
        return verbatim
    return (
        f"[Earlier conversation — summarized]\n{summary}\n\n"
        f"[Most recent messages — verbatim]\n{verbatim}"
    )


def fetch_recent(channel_id, before_id):
    """Context window before `before_id`, oldest-first. Flat last-N lines by
    default; tiered summary+verbatim when COMPACT_HISTORY is on. Same token /
    endpoint as fetch_messages — no extra permissions. Never raises: on error
    it falls back to the flat window (or empty)."""
    try:
        if COMPACT_HISTORY:
            return _fetch_recent_tiered(channel_id, before_id)
        return _fetch_recent_flat(channel_id, before_id)
    except Exception as exc:
        print(f"[bridge] fetch_recent failed for {channel_id}: {exc}", flush=True)
        try:
            return _fetch_recent_flat(channel_id, before_id)
        except Exception:
            return ""


def _recent_lines(channel_id, limit=12):
    """Last `limit` messages of any channel as plain lines (for linked `watch`)."""
    try:
        with httpx.Client(timeout=20, headers=HEADERS) as client:
            r = client.get(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                params={"limit": limit},
            )
        if r.status_code != 200:
            return ""
        return "\n".join(filter(None, (_msg_line(m) for m in reversed(r.json()))))
    except Exception:
        return ""


def _watched_context(channel_id):
    """Recent messages from channels this channel is linked to `watch`, so the bot
    automatically SEES the connected channel — no prompt hardcoding, data-driven."""
    link = _channel_links().get(str(channel_id))
    if not link:
        return ""
    parts = []
    for w in link.get("watch", []):
        lines = _recent_lines(str(w))
        if lines:
            parts.append(f"--- recent messages in linked channel {w} ---\n{lines}")
    return "\n\n".join(parts)


def build_context(channel_id, before_id):
    """This channel's own context, plus any linked `watch` channels' recent msgs."""
    own = _own_context(channel_id, before_id)
    watched = _watched_context(channel_id)
    return "\n\n".join(p for p in (watched, own) if p)


def _own_context(channel_id, before_id):
    """Context to feed the model. With a live session + a known watermark, inject
    ONLY the messages that arrived since the bot's last turn — the gap the session
    doesn't already have — so nothing is missed and nothing is re-fed. Otherwise
    (first message / no session / no watermark) seed the normal recent window.
    Never raises: falls back to the window on any error."""
    cursor = get_context_cursor(channel_id)
    if SESSION_RESUME and cursor and get_session(channel_id):
        try:
            gap = [
                m for m in _fetch_after(channel_id, cursor, HISTORY_DEEP)
                if int(m["id"]) < int(before_id)
                and (m.get("author") or {}).get("id") != BOT_ID
            ]
            gap = _drop_marked(gap)
            if not gap:
                return ""  # nothing new since last reply — the session has it all
            if COMPACT_HISTORY and HISTORY_VERBATIM > 0 and len(gap) > HISTORY_LIMIT:
                older, tail = gap[:-HISTORY_VERBATIM], gap[-HISTORY_VERBATIM:]
                summary = _compact_summary(channel_id, older)
                head = f"[Summary of activity since your last reply]\n{summary}\n\n" if summary else ""
                body = head + "\n".join(filter(None, (_msg_line(m) for m in tail)))
            else:
                body = "\n".join(filter(None, (_msg_line(m) for m in gap)))
            body = body.strip()
            return (
                "New messages in this channel since your last reply "
                f"(you haven't seen these yet):\n{body}"
            ) if body else ""
        except Exception as exc:
            print(f"[bridge] incremental context failed for {channel_id}: {exc}", flush=True)
    return fetch_recent(channel_id, before_id)


def fetch_latest_id(channel_id):
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        response = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": 1},
        )
    if response.status_code == 403:
        DENIED.add(channel_id)
        return "0"
    response.raise_for_status()
    messages = response.json()
    if isinstance(messages, list) and messages:
        return messages[0]["id"]
    return "0"


def _strip_leading_mention(content, user_id):
    """Drop a mention of user_id that the model already put at the start of its
    reply, so the bridge doesn't prepend a second one (double @)."""
    text = content.lstrip()
    toks = (f"<@{user_id}>", f"<@!{user_id}>")
    changed = True
    while changed:
        changed = False
        for tok in toks:
            if text.startswith(tok):
                text = text[len(tok):].lstrip()
                changed = True
    return text


def post(channel_id, content, mention_user_id=None):
    if mention_user_id:
        content = f"<@{mention_user_id}> {_strip_leading_mention(content, mention_user_id)}"
    # Build the allowed-mentions whitelist from the user ids ACTUALLY present in
    # the text (any <@id> / <@!id> the model wrote), plus the reply target. We
    # don't use {"parse": ["users"]} blanket because that would also let stray
    # ids in quoted/example text ping people; whitelisting only ids we see in
    # our own content keeps it deliberate while still letting CowBot @ others.
    ids = set(re.findall(r"<@!?(\d+)>", content))
    if mention_user_id:
        ids.add(str(mention_user_id))
    allowed_mentions = {"users": sorted(ids)} if ids else {"parse": []}
    payload = {
        "content": content[:2000],
        "allowed_mentions": allowed_mentions,
    }
    with httpx.Client(timeout=20, headers=POST_HEADERS) as client:
        response = client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            json=payload,
        )
        response.raise_for_status()


def react(channel_id, message_id, emoji="👀"):
    """Best-effort emoji ack so the sender knows the message was seen.

    Returns True if Discord confirmed it (2xx), else False. We check the status
    and retry once on a 429 (rate limit) — the ✅ done-mark in particular must
    survive a transient hiccup, otherwise the message ends up with no ack at all.
    """
    url = (
        f"https://discord.com/api/v10/channels/{channel_id}/messages/"
        f"{message_id}/reactions/{quote(emoji)}/@me"
    )
    for attempt in range(2):
        try:
            with httpx.Client(timeout=10, headers=HEADERS) as client:
                resp = client.put(url)
            if resp.status_code < 300:
                return True
            if resp.status_code == 429 and attempt == 0:
                try:
                    time.sleep(min(5.0, float(resp.json().get("retry_after", 1))))
                except Exception:
                    time.sleep(1.0)
                continue
        except Exception:
            pass
        break
    return False


def unreact(channel_id, message_id, emoji):
    """Remove our own reaction (to swap the working ack for a done one once the
    reply is posted). Checks status and retries once on 429 — same as react():
    a swallowed failure here leaves BOTH the 👀/⏳ and the ✅ on the message,
    which looks half-done. Returns True if Discord confirmed the removal."""
    url = (
        f"https://discord.com/api/v10/channels/{channel_id}/messages/"
        f"{message_id}/reactions/{quote(emoji)}/@me"
    )
    for attempt in range(2):
        try:
            with httpx.Client(timeout=10, headers=HEADERS) as client:
                resp = client.delete(url)
            if resp.status_code < 300 or resp.status_code == 404:
                return True  # 404 = already gone, also fine
            if resp.status_code == 429 and attempt == 0:
                try:
                    time.sleep(min(5.0, float(resp.json().get("retry_after", 1))))
                except Exception:
                    time.sleep(1.0)
                continue
        except Exception:
            pass
        break
    return False


def delete_message(channel_id, message_id):
    """Best-effort delete. True on success; False on failure (e.g. 403 = we lack
    Manage Messages for someone else's message)."""
    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            r = client.delete(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}"
            )
        if r.status_code in (200, 204):
            return True
        if r.status_code == 403:
            print(f"[bridge] delete forbidden {channel_id}/{message_id} (need Manage Messages)", flush=True)
        return False
    except Exception as exc:
        print(f"[bridge] delete failed {channel_id}/{message_id}: {exc}", flush=True)
        return False


def _touch_activity():
    """Record that a human just posted, for activity-driven schedulers (runtime
    proactive review). Best-effort."""
    try:
        with open(ACTIVITY_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _fetch_latest_raw(channel_id, limit):
    """Latest `limit` raw message objects, oldest-first. Empty on error/denied."""
    if channel_id in DENIED:
        return []
    with httpx.Client(timeout=20, headers=HEADERS) as client:
        r = client.get(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            params={"limit": min(100, max(1, limit))},
        )
    if r.status_code == 403:
        DENIED.add(channel_id)
        return []
    r.raise_for_status()
    batch = r.json()
    if not isinstance(batch, list):
        return []
    return sorted(batch, key=lambda m: int(m["id"]))


def sweep_marked_deletions(channel_id):
    """Sweep: delete messages reacted with DELETE_MARK_EMOJI (plus the bot's reply
    run right after a marked user message). No-op unless DELETE_MARKED is on.
    Never raises."""
    if not DELETE_MARKED:
        return
    try:
        msgs = _fetch_latest_raw(channel_id, DELETE_SWEEP_LIMIT)
        if not msgs:
            return
        n = len(msgs)
        targets = []
        for i, m in enumerate(msgs):
            if _has_delete_mark(m):
                targets.append(m)
                if (m.get("author") or {}).get("id") != BOT_ID:
                    j = i + 1
                    while j < n and (msgs[j].get("author") or {}).get("id") == BOT_ID:
                        targets.append(msgs[j])
                        j += 1
        seen = set()
        for m in targets:
            mid = m["id"]
            if mid in seen or mid in _DELETE_SKIP:
                continue
            seen.add(mid)
            if not delete_message(channel_id, mid):
                _DELETE_SKIP.add(mid)   # don't hammer un-deletable messages every tick
    except Exception as exc:
        print(f"[bridge] sweep_marked_deletions failed for {channel_id}: {exc}", flush=True)


def _typing_burst(channel_id, reply_len):
    """Fire the typing indicator ONCE, right before we post, so it shows up only
    while we're actually 'typing out' the answer — not for the whole long run
    (that's what the 👀 reaction + progress posts are for). One typing POST lasts
    ~10s in Discord and is cleared the moment the message lands, so we trigger it
    and pause briefly (scaled to reply length) to make the appearance natural."""
    try:
        with httpx.Client(timeout=10, headers=HEADERS) as client:
            client.post(f"https://discord.com/api/v10/channels/{channel_id}/typing")
    except Exception:
        pass
    # ~1s floor, +1s per 400 chars, capped at 4s so we never stall a reply long.
    time.sleep(min(4.0, max(1.0, reply_len / 400.0)))


def download_attachments(msg):
    """Download a message's attachments to disk so Claude can read them (vision
    for images, text/PDF for files). Returns local paths."""
    atts = msg.get("attachments") or []
    if not atts:
        return []
    base = os.path.join(os.environ.get("TMPDIR", "/tmp"), "attachments", str(msg["id"]))
    os.makedirs(base, exist_ok=True)
    paths = []
    for a in atts:
        url, name = a.get("url"), (a.get("filename") or "file")
        if not url or (a.get("size") or 0) > ATTACH_MAX_BYTES:
            continue
        try:
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
            path = os.path.join(base, name)
            with open(path, "wb") as f:
                f.write(resp.content)
            paths.append(path)
        except Exception as exc:
            print(f"[bridge] attachment download failed ({name}): {exc}", flush=True)
    return paths


def _split_message(text, limit=None):
    """Split text into <=limit chunks on line boundaries; hard-split long lines."""
    limit = limit or REPLY_CHUNK
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if not cur:
            cur = line
        elif len(cur) + 1 + len(line) <= limit:
            cur += "\n" + line
        else:
            chunks.append(cur)
            cur = line
    if cur:
        chunks.append(cur)
    return chunks


def post_file(channel_id, content, filename, file_text, mention_user_id=None):
    """Post a short message with the full text attached as a file."""
    allowed = {"parse": []}
    if mention_user_id:
        allowed = {"users": [mention_user_id]}
        content = f"<@{mention_user_id}> {content}"
    data = {"payload_json": json.dumps({"content": content[:2000], "allowed_mentions": allowed})}
    files = {"files[0]": (filename, file_text.encode("utf-8"), "text/markdown")}
    with httpx.Client(timeout=30, headers=HEADERS) as client:  # auth only; httpx sets multipart
        resp = client.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=data, files=files,
        )
        resp.raise_for_status()


def post_reply(channel_id, content, mention_user_id=None):
    """Post a reply, splitting long output across messages (or a file) instead of
    silently truncating at 2000 chars."""
    content = (content or "").strip() or "Claude returned no output."
    if len(content) <= 2000:
        post(channel_id, content, mention_user_id)
    elif len(content) <= REPLY_FILE_THRESHOLD:
        for i, chunk in enumerate(_split_message(content)):
            post(channel_id, chunk, mention_user_id if i == 0 else None)
    else:
        post_file(channel_id, "📄 回复较长,完整内容见附件:", "reply.md", content, mention_user_id)


def log_usage(channel_id, author, data):
    try:
        rec = {
            "ts": round(time.time()),
            "channel": str(channel_id),
            "author": author,
            "cost_usd": data.get("total_cost_usd"),
            "turns": data.get("num_turns"),
            "duration_ms": data.get("duration_ms"),
        }
        with open(USAGE_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def build_status(guild_id=None):
    up = int(time.time() - START_TIME)
    h, rem = divmod(up, 3600)
    m = rem // 60
    total, n = 0.0, 0
    try:
        with open(USAGE_FILE) as f:
            for line in f:
                try:
                    total += json.loads(line).get("cost_usd") or 0.0
                    n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    # Count only THIS server's channels — never expose the cross-server total.
    try:
        list_text_channels()  # refresh CHANNEL_GUILD
        watched = sum(1 for g in CHANNEL_GUILD.values() if g == str(guild_id)) if guild_id else "?"
    except Exception:
        watched = "?"
    limit_line = ""
    if is_limited():
        limit_line = f"\n• ⏳ 额度受限,约 {fmt_utc(limited_until())} 恢复(已排队 {len(list_deferred())} 条)"
    return (
        "**Status**\n"
        f"• model: `{CLAUDE_MODEL or 'default'}` · effort: `{CLAUDE_EFFORT or 'default'}` · perms: `{PERMISSION_MODE}`\n"
        f"• session resume: {'on' if SESSION_RESUME else 'off'} · watched: {watched} channels\n"
        f"• uptime: {h}h{m}m · handled runs: {n} · 累计成本: ${total:.2f}"
        f"{limit_line}"
    )


class RateLimited(Exception):
    def __init__(self, reset_epoch):
        super().__init__("rate limited")
        self.reset_epoch = reset_epoch


def _looks_rate_limited(text):
    t = (text or "").lower()
    return any(k in t for k in ("session limit", "usage limit", "rate limit", "429"))


def parse_reset_epoch(text):
    """Parse 'resets 1:30am (UTC)' → epoch of the next such UTC time. If we can't
    parse it, fall back to now + LIMIT_DEFAULT_COOLDOWN so we still auto-retry."""
    now = datetime.datetime.now(datetime.timezone.utc)
    m = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", (text or "").lower())
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ap = m.group(3)
        if ap == "pm" and hh != 12:
            hh += 12
        elif ap == "am" and hh == 12:
            hh = 0
        if 0 <= hh < 24 and 0 <= mm < 60:
            target = now.replace(hour=hh, minute=mm, second=30, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            if (target - now).total_seconds() <= 25 * 3600:
                return target.timestamp()
    return now.timestamp() + LIMIT_DEFAULT_COOLDOWN


def set_limited(reset_epoch):
    try:
        with open(LIMITED_FILE, "w") as f:
            f.write(str(reset_epoch))
    except Exception:
        pass


def limited_until():
    try:
        with open(LIMITED_FILE) as f:
            return float(f.read().strip())
    except Exception:
        return 0.0


def is_limited():
    return time.time() < limited_until()


def fmt_utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%H:%M UTC")


def defer_message(channel_id, msg, prompt):
    """Queue a request to be answered once the rate limit resets."""
    os.makedirs(DEFERRED_DIR, exist_ok=True)
    author = msg.get("author") or {}
    rec = {
        "channel_id": str(channel_id),
        "message_id": str(msg["id"]),
        "author_id": author.get("id"),
        "author": author.get("username"),
        "is_bot": bool(author.get("bot")) and author.get("id") != BOT_ID,
        "guild_id": msg.get("guild_id") or CHANNEL_GUILD.get(str(channel_id)),
        "prompt": prompt,
    }
    dst = os.path.join(DEFERRED_DIR, f"{msg['id']}.json")
    tmp = dst + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, dst)


def list_deferred():
    try:
        return sorted(
            os.path.join(DEFERRED_DIR, n)
            for n in os.listdir(DEFERRED_DIR)
            if n.endswith(".json")
        )
    except FileNotFoundError:
        return []


def drain_deferred():
    """After the limit resets, do ONE coherent catch-up per channel — read the
    backlog (via the context window) and respond to the current state, addressing
    open items one at a time — rather than replaying every stale message. Stops
    (keeping the rest) if we get rate-limited again."""
    groups = {}
    for path in list_deferred():
        try:
            with open(path) as f:
                rec = json.load(f)
            rec["_path"] = path
            groups.setdefault(rec["channel_id"], []).append(rec)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
    for ch, recs in groups.items():
        if is_limited():
            return  # still limited — leave the rest for next time
        recs.sort(key=lambda r: int(r.get("message_id") or 0))
        latest = recs[-1]
        catchup = (
            "[You were rate-limited and just came back online. The recent channel "
            "activity is in your context — catch up on what happened while you were "
            "out and respond to what currently needs you, one item at a time. Do "
            "NOT reply to each old message separately, and do NOT @ other bots just "
            "to acknowledge.]\n\n" + (latest.get("prompt") or "")
        )
        try:
            with channel_lock(ch):
                reply = run_claude(latest.get("author") or "user", ch, catchup,
                                   guild_id=latest.get("guild_id"))
        except RateLimited:
            return
        except subprocess.TimeoutExpired:
            reply = f"⏱️ 排队的任务超过 {TIMEOUT_SECONDS // 60} 分钟上限,被中断。"
        except Exception as exc:
            reply = f"Bridge error: {exc}"
        # Don't @ a bot author on resume either (avoid re-triggering the loop); the
        # reply itself can mention a specific bot if CowBot decides it's needed.
        mention = None if latest.get("is_bot") else latest.get("author_id")
        try:
            post_reply(ch, reply, mention_user_id=mention)
            mid = latest.get("message_id")
            if mid and react(ch, mid, "✅"):
                unreact(ch, mid, "⏳")
                unreact(ch, mid, "👀")
        finally:
            for r in recs:  # clear the whole channel's queue (handled in one pass)
                try:
                    os.remove(r["_path"])
                except OSError:
                    pass


def ensure_server_dir(guild_id):
    """Per-server private working directory. Each server's claude runs here (cwd)
    with its own session store + TMPDIR, so one server's scratch/clones are not in
    another server's working dir. CLAUDE.md is symlinked in so it still loads."""
    name = str(guild_id) if guild_id else "_unknown"
    base = os.path.join(SERVERS_DIR, name)
    for d in (base, os.path.join(base, ".claude"), os.path.join(base, "tmp")):
        os.makedirs(d, exist_ok=True)
    link = os.path.join(base, "CLAUDE.md")
    src = os.path.join(os.path.dirname(ROOT), "CLAUDE.md")  # /app/CLAUDE.md
    try:
        if not os.path.lexists(link):
            os.symlink(src, link)
    except OSError:
        pass
    return base


def ensure_channel_dir(guild_id, channel_id):
    """Per-CHANNEL private working dir: servers/<guild>/channels/<channel>/. Each
    channel's claude runs here (cwd + CLAUDE_CONFIG_DIR) so different channels in the
    SAME server keep SEPARATE memory/sessions. Falls back to the server dir when
    channel_id is missing (e.g. DMs). CLAUDE.md symlinked in so it still loads.

    Config-driven link: if channel_id is in `.channel_links.json`, redirect to the
    target context's dir so the linked channel SHARES memory + session with it
    (target may be another server's channel, or a whole guild if "channel" omitted)."""
    link = _channel_links().get(str(channel_id)) if channel_id else None
    if link:
        guild_id = link.get("guild") or guild_id
        channel_id = link.get("channel")  # None -> guild-level target (server dir)
    base = ensure_server_dir(guild_id)
    if not channel_id:
        return base
    cbase = os.path.join(base, "channels", str(channel_id))
    for d in (cbase, os.path.join(cbase, ".claude"), os.path.join(cbase, "tmp")):
        os.makedirs(d, exist_ok=True)
    link = os.path.join(cbase, "CLAUDE.md")
    src = os.path.join(os.path.dirname(ROOT), "CLAUDE.md")
    try:
        if not os.path.lexists(link):
            os.symlink(src, link)
    except OSError:
        pass
    return cbase


_run_lock = threading.Lock()


def write_run(run_id, **fields):
    """Update an agent run's live telemetry file (read by monitor.py)."""
    try:
        os.makedirs(RUNS_DIR, exist_ok=True)
        path = os.path.join(RUNS_DIR, run_id + ".json")
        with _run_lock:
            data = {}
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            data.update(fields)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
    except Exception:
        pass


def clear_run(run_id):
    try:
        os.remove(os.path.join(RUNS_DIR, run_id + ".json"))
    except OSError:
        pass


class _Res:
    """A subprocess.run-compatible result so the streaming path is a drop-in."""
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _stream_claude(cmd, cwd, env, run_id):
    """Run claude with stream-json, writing live phase/thinking to the run's
    telemetry as events arrive. Returns a result whose stdout is the final
    `result` event (so the caller's json parsing is unchanged). Raises
    subprocess.TimeoutExpired on timeout, like subprocess.run did."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
    )
    write_run(run_id, pid=proc.pid, status="running", phase="starting", updated=time.time())
    final = None
    think = ""
    last_write = 0.0
    timed_out = {"v": False}

    def _kill():
        timed_out["v"] = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(TIMEOUT_SECONDS, _kill)
    timer.start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = ev.get("type")
            phase = None
            if t == "stream_event":
                d = (ev.get("event") or {}).get("delta") or {}
                if d.get("type") == "thinking_delta":
                    think += d.get("thinking", "")
                    phase = "💭 thinking"
                elif d.get("type") == "text_delta":
                    think += d.get("text", "")
                    phase = "✍️ replying"
            elif t == "assistant":
                for blk in (ev.get("message") or {}).get("content") or []:
                    bt = blk.get("type")
                    if bt == "tool_use":
                        nm = blk.get("name", "tool")
                        inp = blk.get("input") or {}
                        arg = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
                        think = f"{nm}: {str(arg)[:140]}"
                        phase = f"🔧 {nm}"
                    elif bt == "thinking":
                        think = blk.get("thinking", "")
                        phase = "💭 thinking"
                    elif bt == "text":
                        think = blk.get("text", "")
                        phase = "✍️ replying"
            elif t == "result":
                final = ev
                phase = "done"
            if phase and (phase == "done" or time.time() - last_write > 0.35):
                write_run(run_id, phase=phase, thinking=think[-800:], updated=time.time())
                last_write = time.time()
        proc.wait()
    finally:
        timer.cancel()
    if timed_out["v"]:
        raise subprocess.TimeoutExpired(cmd, TIMEOUT_SECONDS)
    try:
        stderr = proc.stderr.read() or ""
    except Exception:
        stderr = ""
    return _Res(proc.returncode, json.dumps(final) if final else "", stderr)


def run_claude(author, channel_id, prompt, history="", guild_id=None):
    context_block = (
        f"Recent channel messages (oldest first, for context only):\n{history}\n\n"
        if history
        else ""
    )
    instruction = (
        "You are Lingfei 的专属 bot replying to a Discord message in this server. Your "
        "context, capabilities, and the /app/src/discord_api.py toolbox are in "
        "CLAUDE.md (already loaded).\n"
        "You work freely across ALL the servers and channels you're in — treat "
        "them as one connected workspace, cooperate fully, and never deflect a "
        "cross-server request with \"I only work in this server\". You can read or "
        "post to any channel by id with the toolbox, regardless of which server it "
        "is in. Only hard rule: never paste tokens/secrets into a message.\n"
        "Linked channels' recent messages may be shown to you for context — "
        "use them ONLY when relevant to the current request. Do NOT proactively "
        "or repeatedly bring up other channels, recap them, or announce what is "
        "happening elsewhere unless the user explicitly asks.\n"
        "Use the recent messages for context; act on the new Message; reply "
        "concisely in plain text. The context block may be truncated or "
        "summarized — if you need earlier detail it omits, fetch more yourself "
        f"with `python /app/src/discord_api.py read {channel_id} --limit N` "
        "(paging further back as needed). If this is more than a quick reply, post brief "
        "progress updates to this channel as you work "
        f"(`python /app/src/discord_api.py post {channel_id} \"...\"`); the bridge "
        "only posts your final answer.\n\n"
        f"Sender: {author}\nThis channel: {channel_id}\n\n"
        f"{context_block}New Message:\n{prompt}"
    )
    if STREAM_TELEMETRY:
        base = [CLAUDE_BIN, "-p", "--permission-mode", PERMISSION_MODE,
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    else:
        base = [CLAUDE_BIN, "-p", "--permission-mode", PERMISSION_MODE, "--output-format", "json"]
    if CLAUDE_MODEL:
        base.extend(["--model", CLAUDE_MODEL])
    if CLAUDE_EFFORT:
        base.extend(["--effort", CLAUDE_EFFORT])
    # Per-CHANNEL isolation: run in this channel's private dir (cwd), with its own
    # memory/session store + TMPDIR, so different channels in the same server do
    # NOT share memory. (var kept as server_dir downstream; it is now the channel dir.)
    server_dir = ensure_channel_dir(guild_id, channel_id)
    sub_env = dict(
        os.environ,
        AGENT_CURRENT_GUILD=str(guild_id or ""),
        AGENT_SERVER_DIR=server_dir,
        CLAUDE_CONFIG_DIR=os.path.join(server_dir, ".claude"),
        TMPDIR=os.path.join(server_dir, "tmp"),
    )

    run_id = f"{channel_id}.{os.getpid()}.{int(time.time() * 1000)}"
    write_run(run_id, server=str(guild_id or ""), channel=str(channel_id),
              user=str(author), model=CLAUDE_MODEL or "default", effort=CLAUDE_EFFORT or "default",
              start=time.time(), status="starting", phase="starting", thinking="")

    def invoke(resume_id):
        cmd = list(base)
        if resume_id:
            cmd.extend(["--resume", resume_id])
        cmd.append(instruction)
        if STREAM_TELEMETRY:
            return _stream_claude(cmd, server_dir, sub_env, run_id)
        return subprocess.run(
            cmd, cwd=server_dir, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS, env=sub_env,
        )

    try:
        resume_id = get_session(channel_id) if SESSION_RESUME else None
        result = invoke(resume_id)
        # The stored session can go missing (e.g. config dir reset) — retry fresh.
        if result.returncode != 0 and resume_id:
            print(f"[bridge] resume failed for {channel_id}; starting a fresh session", flush=True)
            clear_session(channel_id)
            result = invoke(None)

        if result.returncode != 0:
            err = (result.stderr or result.stdout).strip()
            try:
                edata = json.loads(err)
                err = edata.get("result") or edata.get("error") or err
            except Exception:
                pass
            if _looks_rate_limited(err):
                reset = parse_reset_epoch(err)
                set_limited(reset)
                raise RateLimited(reset)
            return f"⚠️ {err[:MAX_RESPONSE_CHARS]}"

        try:
            data = json.loads(result.stdout or "{}")
        except Exception:
            return (result.stdout or "").strip() or "Claude returned no output."
        # A 0-exit result can still be a rate-limit error (is_error + 429).
        if data.get("is_error") and (data.get("api_error_status") == 429 or _looks_rate_limited(data.get("result"))):
            reset = parse_reset_epoch(data.get("result"))
            set_limited(reset)
            raise RateLimited(reset)
        if SESSION_RESUME and data.get("session_id"):
            set_session(channel_id, data["session_id"])
        log_usage(channel_id, author, data)
        # Full reply — post_reply() chunks/uploads it instead of truncating.
        return (data.get("result") or "").strip() or "Claude returned no output."
    finally:
        clear_run(run_id)


def handle_message(channel_id, msg):
    author = msg.get("author") or {}
    author_id = author.get("id", "")
    if author_id == BOT_ID:
        return
    if ALLOWED_USER_IDS and author_id not in ALLOWED_USER_IDS:
        return
    content = msg.get("content", "") or ""
    _wl = load_channel_whitelist()
    _in_whitelist = bool(_wl) and str(channel_id) in _wl
    # Whitelist channels: respond to all messages (no @mention needed).
    # Non-whitelist channels: require @mention as before.
    if not _in_whitelist and not is_addressed(content):
        return
    if not claim_message(msg["id"]):
        return  # already handled by the other path (poller/gateway)
    react(channel_id, msg["id"], "👀")  # instant ack — before any gating/judging
    # Distinguish humans from bots/webhooks (Discord sets author.bot). A human
    # message clears the bot-loop streak; a bot message goes through the anti-loop
    # guard so two bots can't @-pingpong forever.
    is_bot_author = bool(author.get("bot")) and author_id != BOT_ID
    cid = str(channel_id)
    if is_bot_author:
        streak = _bot_reply_streak.get(cid, 0)
        # 1) Catastrophic ceiling only (token-burn breaker / bots disabled). Real
        #    collaboration shouldn't reach it; the progress gates below do the work.
        if not REPLY_TO_BOTS or BOT_REPLY_MAX <= 0 or streak >= BOT_REPLY_MAX:
            react(channel_id, msg["id"], BOT_ACK_EMOJI)
            print(f"[bridge] bot ceiling in {channel_id} (streak={streak}); react-only",
                  flush=True)
            return
        # 2) Stall/repetition detector — the real loop signature. Near-duplicate
        #    bot turns pile up a stall count; any novel/progressing message resets
        #    it, so a genuinely advancing task (varied content) is never cut off
        #    no matter how many turns it takes.
        norm = WS_RE.sub(" ", content.lower()).strip()
        recent = _bot_recent_msgs.get(cid, [])
        rep = max((difflib.SequenceMatcher(None, norm, r).ratio() for r in recent), default=0.0)
        stall = _bot_stall.get(cid, 0) + 1 if (norm and rep >= BOT_REPEAT_RATIO) else 0
        _bot_stall[cid] = stall
        _bot_recent_msgs[cid] = (recent + [norm])[-6:]
        if BOT_STALL_MAX > 0 and stall >= BOT_STALL_MAX:
            react(channel_id, msg["id"], BOT_ACK_EMOJI)
            print(f"[bridge] bot stuck/repeating in {channel_id} (stall={stall}); react-only",
                  flush=True)
            return
        # 3) Content judgment: engage only if the bot message is genuine work; if
        #    it's idle chatter / no-progress filler, just react and let it die.
        if BOT_JUDGE:
            try:
                hist = "\n".join(filter(None, (
                    _msg_line(m) for m in _fetch_before(channel_id, msg["id"], 6))))
            except Exception:
                hist = ""
            if not _bot_should_engage(content, hist):
                react(channel_id, msg["id"], BOT_ACK_EMOJI)
                print(f"[bridge] bot msg judged chatter in {channel_id}; react-only",
                      flush=True)
                return
        _bot_reply_streak[cid] = streak + 1
    else:
        _bot_reply_streak.pop(cid, None)
        _bot_stall.pop(cid, None)
        _bot_recent_msgs.pop(cid, None)
    # We DO @ the bot back when engaging — the other bot needs the mention to be
    # triggered, so real back-and-forth collaboration can continue. The loop is
    # broken by the content judgment above (chatter → we post nothing, just react)
    # plus the BOT_REPLY_MAX safety cap, NOT by withholding the @.
    reply_mention = author_id
    # Which server this is in (gateway events carry guild_id; poller uses the map).
    guild_id = msg.get("guild_id") or CHANNEL_GUILD.get(str(channel_id))
    prompt = clean_prompt(content)
    low = prompt.strip().lower()
    # Built-in commands (no Claude run).
    if low in ("/reset", "/new", "reset session"):
        clear_session(channel_id)
        clear_context_cursor(channel_id)
        post(channel_id, "🔄 已重置本频道的会话,下一条消息从头开始。", mention_user_id=reply_mention)
        return
    if low in ("/help", "help"):
        post_reply(channel_id, HELP_TEXT, mention_user_id=reply_mention)
        return
    if low in ("/status", "status"):
        post_reply(channel_id, build_status(guild_id), mention_user_id=reply_mention)
        return
    # Whitelist management commands.
    if low == "/whitelist" or low.startswith("/whitelist "):
        wl = load_channel_whitelist()
        parts = prompt.strip().split(None, 2)
        sub = parts[1].lower() if len(parts) > 1 else "show"
        arg = parts[2].strip() if len(parts) > 2 else ""
        if sub == "show":
            if wl:
                lines = "\n".join(f"• <#{c}> ({c})" for c in sorted(wl))
                msg = f"**频道白名单** ({len(wl)} 个):\n{lines}"
            else:
                msg = "**频道白名单**:空 — 所有频道都响应 @mention。"
        elif sub == "add" and arg:
            cid_arg = arg.strip("<>#").split(">")[0].split("|")[-1]
            wl.add(cid_arg)
            save_channel_whitelist(wl)
            msg = f"✅ 已加入白名单: <#{cid_arg}> ({cid_arg})。当前共 {len(wl)} 个频道。"
        elif sub == "remove" and arg:
            cid_arg = arg.strip("<>#").split(">")[0].split("|")[-1]
            if cid_arg in wl:
                wl.discard(cid_arg)
                save_channel_whitelist(wl)
                msg = f"✅ 已从白名单移除: <#{cid_arg}>。剩余 {len(wl)} 个频道。"
            else:
                msg = f"⚠️ <#{cid_arg}> 不在白名单里。"
        elif sub == "clear":
            save_channel_whitelist(set())
            msg = "✅ 白名单已清空 — 所有频道恢复响应。"
        else:
            msg = (
                "用法:\n"
                "• `/whitelist show` — 查看当前白名单\n"
                "• `/whitelist add <channel_id>` — 添加频道\n"
                "• `/whitelist remove <channel_id>` — 移除频道\n"
                "• `/whitelist clear` — 清空白名单(恢复全频道响应)"
            )
        post_reply(channel_id, msg, mention_user_id=reply_mention)
        return
    # Pull in any attached images/files so Claude can read them.
    try:
        files = download_attachments(msg)
    except Exception as exc:
        print(f"[bridge] attachment handling failed: {exc}", flush=True)
        files = []
    if files:
        prompt = (prompt or "") + (
            "\n\n[The user attached files — view them with your Read tool: "
            + ", ".join(files) + "]"
        )
    if not prompt:
        prompt = "The user only mentioned you without extra text. Reply briefly and ask what they need."
    if is_bot_author:
        # Let Claude apply its own judgment on top of the hard cap: this came from
        # another bot, so wind the exchange down unless there's real work to do.
        prompt = (
            "[This @-mention is from ANOTHER BOT, not a human. To avoid an endless "
            "bot-to-bot loop: only respond if there is something genuinely "
            "actionable. If not, reply with a single short line and do NOT ask "
            "questions back. Keep it brief.]\n\n" + prompt
        )
    # Rate-limited right now → don't waste a call; queue it and answer on reset.
    if is_limited():
        defer_message(channel_id, msg, prompt)
        react(channel_id, msg["id"], "⏳")
        # NEVER @ another bot with the limited notice — that pings it, it pings
        # back, and we're in an infinite "out of quota" ping-pong. Bots just get the
        # silent ⏳; humans get told to wait.
        if not is_bot_author:
            post(channel_id, f"⏳ Claude 额度暂时用满,约 {fmt_utc(limited_until())} 恢复后我会自动回复你。",
                 mention_user_id=reply_mention)
        return
    print(
        f"[bridge] handling {msg['id']} in {channel_id} "
        f"from {author.get('username', author_id)}",
        flush=True,
    )
    reply = "Bridge error."
    try:
        # Serialize same-channel messages so they continue one conversation in
        # order; different channels still run concurrently.
        with channel_lock(channel_id):
            try:
                history = build_context(channel_id, msg["id"])
            except Exception as exc:
                print(f"[bridge] history fetch failed: {exc}", flush=True)
                history = ""
            try:
                reply = run_claude(author.get("username", author_id), channel_id, prompt, history, guild_id)
                # The session now knows up to this message — next turn injects only
                # what arrives after it (no re-feeding, no gaps).
                set_context_cursor(channel_id, msg["id"])
            except RateLimited as rl:
                # Hit the limit mid-flight — queue it and tell the user it'll auto-resume.
                defer_message(channel_id, msg, prompt)
                react(channel_id, msg["id"], "⏳")
                reply = f"⏳ Claude 额度刚好用满,约 {fmt_utc(rl.reset_epoch)} 恢复后我会自动回复你。"
            except subprocess.TimeoutExpired:
                reply = (
                    f"⏱️ 这条任务超过了 {TIMEOUT_SECONDS // 60} 分钟的处理上限,被中断了。"
                    "可以把它拆小一点,或让我把长任务放后台跑、完成后回报。"
                )
            except Exception as exc:
                reply = f"Bridge error: {exc}"
    finally:
        # Now we have the answer — show "typing…" briefly so it looks like we're
        # writing it out, then post and swap the 👀 ack for a ✅ done mark.
        _typing_burst(channel_id, len(reply))
    post_reply(channel_id, reply, mention_user_id=reply_mention)
    # Swap the working ack for a done mark. Add ✅ FIRST and only drop the 👀
    # once ✅ is confirmed — so a transient failure leaves the 👀 standing
    # rather than a bare, ack-less message (the M1·IR audit reply hit exactly
    # this: ✅ never landed and 👀 was already gone → looked like nothing ran).
    if react(channel_id, msg["id"], "✅"):
        unreact(channel_id, msg["id"], "👀")


def main():
    mode = f"{len(GUILD_IDS)} configured guild(s)" if GUILD_IDS else "all joined guilds"
    print(f"[bridge] started ({mode})", flush=True)
    cursors = load_cursors()
    while True:
        maybe_reload()
        prune_handled()
        try:
            channels = list_text_channels()
        except Exception as exc:
            print(f"[bridge] channel list error: {exc}", flush=True)
            channels = [CHANNEL_ID] if CHANNEL_ID else []
        for channel_id in channels:
            try:
                if channel_id not in cursors:
                    # Newly seen channel: start at the latest id so we don't
                    # replay full history on first run.
                    cursors[channel_id] = fetch_latest_id(channel_id)
                    save_cursors(cursors)
                    continue
                messages = fetch_messages(channel_id, cursors[channel_id])
                human_activity = False
                for msg in messages:
                    cursors[channel_id] = msg["id"]
                    save_cursors(cursors)
                    if (msg.get("author") or {}).get("id") != BOT_ID:
                        human_activity = True
                    handle_message(channel_id, msg)
                if human_activity:
                    _touch_activity()
                # Marked-deletion sweep: only when this channel saw activity, or a
                # coarse fallback so a 🗑️ on a quiet channel is still caught. No
                # more scanning every channel every tick.
                now = time.time()
                if messages or (now - _LAST_MARK_SWEEP.get(channel_id, 0.0) >= MARK_SWEEP_FALLBACK):
                    sweep_marked_deletions(channel_id)
                    _LAST_MARK_SWEEP[channel_id] = now
            except Exception as exc:
                print(f"[bridge] error in {channel_id}: {exc}", flush=True)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
