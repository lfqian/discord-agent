# discord-agent (CowBot)

A self-hosted Discord agent that bridges Discord mentions to the Claude Code CLI.
Mention the bot in any channel or forum thread and it answers — with real shell,
network, and tool access — running headless inside a hardened container.

## What it does
- **Real-time @-mention trigger** over the Discord Gateway (with a REST poller as
  fallback), dispatched to a worker pool so long tasks run concurrently.
- **Online presence** — holds a Gateway connection so the bot shows green.
- **Guild-wide** — reads/acts in any text channel and forum thread, replying in
  the channel it was summoned from.
- **Agentic** — runs as `claude -p` with full shell + network; can read/edit its
  own code (hot-reloads), use the `discord_api.py` toolbox, `ssh`, run pipelines,
  and install what it needs (`sudo apt`).

## Layout
```
src/        Python runtime (bridge, gateway, sweep, toolbox, …)
docs/       DEPLOY.md (full deploy guide), FEATURE-REPORT.md (roadmap)
examples/   .env / secrets / compose templates
CLAUDE.md   the bot's operating context (loaded by claude each run, cwd=/app)
Containerfile · compose.yaml · run-container.sh · autostart.sh   build/run/boot
```

| `src/` file | Role |
|------|------|
| `discord_agent_runtime.py` | Supervisor: launches + watches the bridge, gateway, sweep, status board; materializes `~/.ssh`. |
| `discord_claude_bridge.py` | REST poller + message handler (`claude -p`), hot-reload, sessions, dedup, rate-limit gate, chunking. |
| `discord_gateway.py` | Gateway presence + real-time `@`-mention trigger → worker pool. |
| `discord_sweep.py` / `discord_resume.py` | Hourly proactive sweep · deferred-queue drainer after a rate limit. |
| `discord_api.py` / `subagent.py` | Discord toolbox CLI · long-lived sub-agent (tmux) manager. |
| `discord_poll.py` / `discord_status.py` / `post_message.py` | One-shot poll, live status board, REST post helper. |

## Setup
See **[docs/DEPLOY.md](docs/DEPLOY.md)** for the full deploy guide (Discord setup,
tokens, build, run, autostart, every env var). In short:

```bash
cp examples/.env.example ~/discordAgentWorkspace/.env      # fill in token, ids, OAuth token
cp examples/secrets.env.example ~/discordAgentWorkspace/secrets.env   # optional task creds
container build -t discord-agent:local -f Containerfile .
./run-container.sh
```

## Configuration
Runtime config lives in the workspace `.env` (never committed). Key knobs:
`DISCORD_GUILD_ID` (watch the whole guild), `CLAUDE_MODEL`, `CLAUDE_PERMISSION_MODE`,
`CLAUDE_TIMEOUT_SECONDS`, `HISTORY_LIMIT`, `GATEWAY_WORKERS`, `BOT_ACTIVITY`.

> **Security:** the bot runs with `bypassPermissions` inside the container, so
> anyone who can mention it can make it run commands there. Restrict with
> `BOT_ALLOWED_USER_IDS` and keep secrets in the workspace (gitignored), not in
> the image.
