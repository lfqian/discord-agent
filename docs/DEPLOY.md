# Deploying CowBot

A self-hosted Discord agent that bridges @-mentions to the Claude Code CLI,
running headless inside a hardened container. This is the **authoritative,
current** deploy guide — follow it top to bottom.

> Audience: a person *or* an automated Claude doing the deploy. Commands are
> copy-pasteable; placeholders look like `<THIS>`.

---

## 1. What you're deploying

One container (`discord-agent`) runs a supervisor (`src/discord_agent_runtime.py`)
that spawns and watches three long-lived pieces:

| process | role |
|---|---|
| `discord_claude_bridge.py` | REST poll fallback + the message handler. Spawns a fresh `claude -p` per @-mention, with per-channel session resume, per-server isolation, tiered context, rate-limit auto-resume, reply chunking, attachments, live telemetry. Hot-reloads itself on source change. |
| `discord_gateway.py` | Holds one Gateway WebSocket → keeps the bot **online** and pushes @-mentions in real time to a worker pool (`GATEWAY_WORKERS`, default 4). |
| `discord_status.py` (per tick) | Maintains one live status-board message. |

The repo is **bind-mounted into the container at `/app`** (so edits hot-reload and
persist on the host), and runtime state lives in a separate **workspace** mounted
at `/workspace`. The bot serves **all Discord servers it's invited to**, each
fully isolated (own working dir under `/workspace/servers/<guild_id>`).

Layout: `src/` (Python), `examples/` (config templates), `docs/`, `CLAUDE.md`
(the bot's operating context, loaded by `claude` every run), `Containerfile` /
`run-container.sh` / `autostart.sh` (build/run/boot).

---

## 2. Prerequisites

- **macOS** with Apple's `container` CLI (`which container`). *(Docker/Podman
  also work via `compose.yaml` — substitute `docker compose up -d --build`.)*
- A **Discord application + bot** (see step 3).
- A **Claude Code OAuth token** (see step 4).
- `git`, and this repo checked out somewhere stable (it stays mounted at `/app`).

---

## 3. Discord setup

1. https://discord.com/developers → **New Application** → **Bot** → **Reset
   Token** → copy the **bot token** (`DISCORD_BOT_TOKEN`).
2. Note the **Application ID** = the bot's client id (`DISCORD_BOT_ID`).
3. **Privileged intents: none required.** The bot uses only the non-privileged
   `GUILD_MESSAGES` intent; @-mention messages carry their content without the
   Message Content intent. (Leave privileged intents off.)
4. **Invite the bot** to each server (a server admin opens this, picks the
   server, authorizes):
   ```
   https://discord.com/oauth2/authorize?client_id=<DISCORD_BOT_ID>&scope=bot&permissions=274878032960
   ```
   (Permissions: view channels, send messages + in threads, read history, add
   reactions, embed links, attach files, manage messages for pin/delete.)

---

## 4. Claude Code OAuth token

On any machine where Claude Code is installed and browser-authed:
```bash
claude setup-token        # prints sk-ant-oat01-...
```
Copy it — it goes in the workspace `.env` as `CLAUDE_CODE_OAUTH_TOKEN`.

---

## 5. Configure

Pick a workspace dir (default `~/discordAgentWorkspace`) and create its `.env`:
```bash
mkdir -p ~/discordAgentWorkspace
cp examples/.env.example ~/discordAgentWorkspace/.env
chmod 600 ~/discordAgentWorkspace/.env
```
Edit `~/discordAgentWorkspace/.env` and set at minimum:
```env
DISCORD_BOT_TOKEN=<bot token>
DISCORD_BOT_ID=<application id>
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
DISCORD_GUILD_ID=            # blank = serve ALL invited servers
DISCORD_CHANNEL_ID=<a channel id>   # status-board / single-server fallback
```
Optional pipeline/task credentials (DB DSN, AWS, GH_TOKEN, LLM keys) go in a
separate `~/discordAgentWorkspace/secrets.env` (`cp examples/secrets.env.example`).

See **§10** for every tunable. Sensible defaults work out of the box.

---

## 6. Build the image
```bash
container build -t discord-agent:local -f Containerfile .
```
This installs python, node, the Claude Code CLI, ffmpeg, gh, psql, build tools,
tmux, sudo, and the Python deps (`requirements.txt`: httpx, python-dotenv,
websockets, rich).

---

## 7. Run
```bash
./run-container.sh
```
This (re)creates the `discord-agent` container with both bind mounts, the right
env, and resource limits (defaults 6 CPU / 6 GB — override with `AGENT_CPUS` /
`AGENT_MEM`, e.g. `AGENT_MEM=4g AGENT_CPUS=4 ./run-container.sh`).

`DISCORD_AGENT_SOURCE_HOST` / `DISCORD_AGENT_WORKSPACE_HOST` override the mounted
paths (default: the repo dir and `~/discordAgentWorkspace`).

---

## 8. Auto-start on login (survive reboots)

Apple `container` has no restart policy, so install a LaunchAgent that runs
`autostart.sh` at login (it does `container start`, preserving the writable layer,
or a fresh `run-container.sh` if the container is gone):
```bash
cat > ~/Library/LaunchAgents/com.cowbot.discord-agent.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.cowbot.discord-agent</string>
  <key>ProgramArguments</key><array><string>$(pwd)/autostart.sh</string></array>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$HOME/discordAgentWorkspace/autostart.out.log</string>
  <key>StandardErrorPath</key><string>$HOME/discordAgentWorkspace/autostart.err.log</string>
</dict></plist>
PLIST
launchctl load -w ~/Library/LaunchAgents/com.cowbot.discord-agent.plist
```

---

## 9. Verify
```bash
container logs discord-agent | tail              # expect "[gateway] READY … ONLINE" + "[bridge] started"
container exec -it discord-agent python /app/src/monitor.py   # live dashboard (Ctrl-C to quit)
```
Then **@-mention the bot** in any invited server's channel — it should react 👀
and reply. The `monitor.py` dashboard shows active agents, worker utilization,
sessions, today's cost, and each agent's live thinking/tool-use.

---

## 10. Configuration reference (workspace `.env`)

**Required**
| var | meaning |
|---|---|
| `DISCORD_BOT_TOKEN` | bot token |
| `DISCORD_BOT_ID` | application/client id |
| `CLAUDE_CODE_OAUTH_TOKEN` | from `claude setup-token` |

**Servers / scope**
| var | default | meaning |
|---|---|---|
| `DISCORD_GUILD_ID` | *(blank)* | blank = all invited servers; one id or comma list = restrict |
| `DISCORD_CHANNEL_ID` | — | status-board channel + single-server fallback |
| `DISCORD_ROLE_ID` | *(blank)* | also trigger on this role mention |
| `BOT_ALLOWED_USER_IDS` | *(blank = anyone)* | comma list to restrict who can invoke |
| `GUILD_CACHE_TTL` | 300 | seconds to cache the guild list |

**Claude run**
| var | default | meaning |
|---|---|---|
| `CLAUDE_MODEL` | *(claude default)* | e.g. `opus`, `sonnet`, `haiku` |
| `CLAUDE_EFFORT` | *(default)* | `low` `medium` `high` `xhigh` `max` |
| `CLAUDE_PERMISSION_MODE` | `bypassPermissions` | `dontAsk` blocks all shell |
| `CLAUDE_TIMEOUT_SECONDS` | 240 | per-run wall-clock cap |
| `CLAUDE_CWD` | `/app` | claude's base cwd |
| `STREAM_TELEMETRY` | 1 | stream output for live monitor; 0 = plain json |

**Concurrency / presence**
| var | default | meaning |
|---|---|---|
| `GATEWAY_WORKERS` | 4 | concurrent @-mentions handled at once |
| `BOT_STATUS` / `BOT_ACTIVITY` / `BOT_ACTIVITY_TYPE` | online / `for @mentions` / 3 | presence text |
| `TICK_SECONDS` | 60 | poll-fallback + status interval |

**Context**
| var | default | meaning |
|---|---|---|
| `HISTORY_LIMIT` | 20 | recent messages fed as context |
| `COMPACT_HISTORY` | 0 | 1 = tiered context (verbatim recent + rolling summary) |
| `HISTORY_VERBATIM` / `HISTORY_DEEP` / `COMPACT_MODEL` / `COMPACT_MAX_WORDS` / `COMPACT_TIMEOUT_SECONDS` | 8 / 120 / haiku / 180 / 60 | tiered-context tuning |
| `SESSION_RESUME` | 1 | per-channel `claude --resume` continuity |

**Proactive sweep**
| var | default | meaning |
|---|---|---|
| `SWEEP_INTERVAL_SECONDS` | 3600 | hourly review fallback (0 disables) |
| `SWEEP_MIN_GAP_SECONDS` | 600 | min gap for activity-driven early review |
| `SWEEP_MAX_PER_CHANNEL` | 40 | cap messages per channel per sweep |

**Reactions / moderation**
| var | default | meaning |
|---|---|---|
| `DROP_MARKED_FROM_CONTEXT` / `DROP_MARK_EMOJI` | 0 / ❌ | react to drop a msg from context (no delete) |
| `DELETE_MARKED_MESSAGES` / `DELETE_MARK_EMOJI` | 0 / 🗑️ | react to actually delete on Discord |

**Replies / attachments**
| var | default | meaning |
|---|---|---|
| `REPLY_CHUNK` / `REPLY_FILE_THRESHOLD` | 1900 / 6000 | split long replies / upload as file |
| `ATTACH_MAX_BYTES` | 25 MB | max attachment to download for the bot to read |
| `LIMIT_DEFAULT_COOLDOWN` | 3600 | fallback rate-limit cooldown if reset time unparseable |

---

## 11. Operations

- **Apply code edits**: the bridge & gateway **hot-reload** on source change (no
  restart). Only changes to `discord_agent_runtime.py` need a restart:
  `container stop discord-agent && container start discord-agent`.
- **Apply `.env` changes**: hot-reloaded too (the bridge watches the .env files).
- **Rebuild** (Containerfile / deps changed): `container build -t discord-agent:local -f Containerfile . && ./run-container.sh`.
- **Per-channel reset**: send `/reset` in a channel to start its session fresh.
  `/help`, `/status` are also built in.
- **Monitor**: `container exec -it discord-agent python /app/src/monitor.py`.

## 12. Notes & gotchas

- `claude -p` is invoked once per @-mention; each is fresh except for the
  per-channel resumed session and the files in that server's workspace dir.
- Cost adds up fast with `opus` + concurrency — watch the monitor's daily total;
  drop to `sonnet`/`haiku` or lower `CLAUDE_EFFORT` to economize.
- `bypassPermissions` means anyone who can @ the bot can make it run shell **in
  the container**. The container is the isolation boundary; restrict with
  `BOT_ALLOWED_USER_IDS` if the channel isn't trusted.
- Secrets live only in the workspace (`~/discordAgentWorkspace`), never in git.
- `rich` is required for the monitor and is in `requirements.txt` (baked at build).
