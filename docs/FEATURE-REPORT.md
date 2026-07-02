# CowBot — Feature Research & Roadmap

Assessment of what's worth adding next, grounded in the current code. Effort is
rough (S = hours, M = a day, L = multi-day). Value is for *this* team (a dev crew
running a biotech IR data pipeline that coordinates in Discord).

## Where it stands today
Already shipped: real-time @-mention trigger over the Gateway (+ REST poll
fallback) with a worker pool; online presence; guild-wide read/act incl. forum
threads; agentic `claude -p` with shell+network (`bypassPermissions`); self-editing
hot-reload; per-channel **session resume** (persistent); progress reporting;
hourly **silent proactive sweep**; SSH to fin-agent; self-provisioning container;
container persistence + login autostart. Pushed to GitHub.

So the core agent loop is solid. The gaps are at the **edges**: I/O richness,
long-job handling, discoverability, governance, and hosting.

---

## Tier 1 — Quick wins / real bugs (do these first)

### 1. Long-reply chunking  ·  S  ·  ★★★★
**Gap (real bug):** replies are hard-cut at `MAX_RESPONSE_CHARS=1800`
([bridge:50](discord_claude_bridge.py)) and `content[:2000]` in `post()` — a long
answer is silently **truncated and the rest is lost**. Split replies >2000 chars
into multiple messages (or, for very long output, upload as a `.md`/file
attachment). This is the highest value-to-effort item.

### 2. Attachment & image input (vision)  ·  M  ·  ★★★★
**Gap:** the handler ignores `attachments` on a message — `discord_poll.py`
*already* surfaces their `filename`+`url`, but the bridge/gateway path drops them.
Wire attachments into `handle_message`: download to `$TMPDIR`, and for images pass
them to Claude (vision). Huge for a dev team — paste a **screenshot of a stack
trace / error / chart** and say "fix this", or drop a CSV/log/PDF to analyze.

### 3. Instant reaction-ack  ·  S  ·  ★★★
React `👀` the moment a message is claimed (before the typing/▶ work starts), so
the sender knows it was seen even if the first real output is seconds away. One
`discord_api.py react` call in `handle_message`.

### 4. Cost & usage tracking  ·  S  ·  ★★★
The `--output-format json` result already carries `total_cost_usd` / `num_turns` —
currently discarded ([bridge run_claude](discord_claude_bridge.py)). Log it per
channel/user to `/workspace`, and optionally surface a weekly "$ spent / busiest
channels" note. Cheap insurance now that anyone can trigger heavy runs.

### 5. Built-in `/help` & `/status`  ·  S  ·  ★★
`@bot help` → what it can do; `@bot status` → uptime, model, queue depth, last
sweep. Pure convenience, makes the bot discoverable to teammates.

---

## Tier 2 — Workflow integration (high value for this team)

### 6. Real background-job system  ·  M  ·  ★★★★
Today long jobs rely on prose guidance ("background it"). Formalize it (the
`subagents`/tmux pattern fits): a job registry in `/workspace/jobs/`, `@bot jobs`
to list status, and **auto-report on completion** to the originating channel. Kills
the 30-min-timeout problem for pipeline sweeps / big transcodes properly.

### 7. PR & code-review integration  ·  M  ·  ★★★
`gh` is installed. `@bot review PR #263` → posts an inline review; optionally watch
for new PRs and auto-comment. Natural fit — this team already merges PRs via bots.

### 8. Pipeline / DB query helpers  ·  M  ·  ★★★
Canned skills over `psql $SUPABASE_DB_URL` and the pipeline: "how many events for
ALKS?", "show last sweep rollup", "tail the fin-agent worker log", "re-run discover
for TGTX". Turns the bot into the team's pipeline console.

### 9. Scheduled reminders / user cron  ·  M  ·  ★★
"@bot remind #channel in 2h to check the ALKS rerun" / "every weekday 9am post the
pipeline rollup". A small scheduler in the runtime + a jobs file.

### 10. GitHub/CI webhook → Discord  ·  M  ·  ★★
Post repo/CI events (PR merged, CI red, deploy done) into a channel and let the bot
react/triage. Needs a tiny inbound webhook listener (or `gh` polling).

---

## Tier 3 — Interaction modes

### 11. Slash commands (`/ask`, `/reset`, `/sweep`, `/job`)  ·  M  ·  ★★★
More discoverable than @-mentions (and removes the "for @mentions" presence label).
Needs application-command registration + handling `INTERACTION_CREATE` over the
Gateway we already hold.

### 12. DM support  ·  S–M  ·  ★★
Handle direct messages for private/one-on-one tasks (DM messages carry content;
needs the `DIRECT_MESSAGES` intent and a channel-type branch).

### 13. Reaction-as-trigger  ·  M  ·  ★★
React `🤖`/`📝` on *any* message to invoke "summarize / explain / turn into a task"
without an @. Needs `GUILD_MESSAGE_REACTIONS` intent + `MESSAGE_REACTION_ADD`.

### 14. Voice (finish the half-built path)  ·  L  ·  ★★
`discord_poll.py` already has `.speak_queue` + `.discord_voice_on` scaffolding for
TTS. Could finish TTS read-out and add speech-to-text for voice-channel Q&A. High
effort (voice gateway/Opus); nice-to-have, not core.

---

## Tier 4 — Intelligence & memory

### 15. Worklog / long-term memory  ·  M  ·  ★★★
Session resume covers a single channel's thread; add a durable, greppable project
worklog the bot writes after notable work and reads at the start — "what did we
decide about the video-capture fix three days ago" across channels/restarts.

### 16. Cross-channel / time-range summaries  ·  S  ·  ★★★
"@bot summarize #omega this week" / a Monday weekly digest. Trivial given the
toolbox already reads any channel — mostly a prompt + a date-range fetch.

### 17. Multi-model routing  ·  S  ·  ★★★
Route quick Q&A to `sonnet`/`haiku` and reserve `opus` for heavy agentic work
(speed + cost). A per-message heuristic or a `--model` hint in the prompt.

### 18. Smarter sweep heuristics  ·  M  ·  ★★
Teach the hourly sweep to spot specific signals — questions aimed at humans that
went unanswered, blocked/stale tasks, PRs awaiting review — and act on those
specifically rather than a generic pass.

---

## Tier 5 — Reliability, governance, hosting

### 19. Always-on hosting (migrate off the laptop)  ·  M  ·  ★★★★
The bot sleeps when the laptop lid closes. Deploy the container to **fin-agent**
(or a small cloud VM) for true 24/7. Biggest reliability win; everything else is
moot if it's only up while the laptop is awake.

### 20. Audit log  ·  S  ·  ★★★
Given `bypassPermissions` + open access, record every command run / file changed /
message posted to `/workspace/audit.log` (and optionally a private channel). Cheap,
and the right hygiene for an agent that can `sudo` and `ssh`.

### 21. Confirmation gate for destructive ops  ·  M  ·  ★★★
Require a reaction-confirm before `rm -rf`, force-push, prod-DB writes, etc. Lets
you keep full autonomy for normal work while putting a speed-bump on the dangerous
20%.

### 22. Per-user permission tiers  ·  S  ·  ★★
Beyond the all-or-nothing `BOT_ALLOWED_USER_IDS`: e.g. everyone can chat/read, only
a named list can run shell / touch prod. One allow-map + a check in `handle_message`.

### 23. Health watchdog + state backup  ·  S  ·  ★★
The runtime already supervises children; add a heartbeat/alert if a process flaps,
and a periodic backup of `/workspace` (sessions, cursors, ssh, configs) so a wiped
container can be restored.

---

## Recommended order
1. **Tier-1 quick wins** (#1 chunking, #2 attachments, #3 ack, #4 cost) — a day,
   each fixes a felt gap.
2. **#19 always-on hosting** — until this lands, the bot is only as reliable as the
   laptop being open.
3. **#6 background jobs + #20 audit log** — make heavy/autonomous work safe and
   non-blocking.
4. Then pick from Tier 2/4 by what the team actually reaches for (PR review, DB
   queries, summaries are the likely hits).
