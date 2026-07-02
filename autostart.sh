#!/bin/sh
# Bring CowBot's container back at login / after a host reboot.
# Apple `container` has no restart policy, so without this the bot stays down
# after a reboot. Prefer `container start` (keeps the writable layer = anything
# the agent `sudo apt install`'d); fall back to a fresh create.
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LOG="$HOME/discordAgentWorkspace/autostart.log"
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "$(date '+%F %T') autostart firing" >> "$LOG"

# Ensure the container service is up (idempotent; no-op if already running).
container system start >> "$LOG" 2>&1 || true
sleep 3

if container ls 2>/dev/null | grep -q '^discord-agent[[:space:]]'; then
    echo "$(date '+%F %T') already running — nothing to do" >> "$LOG"
elif container ls -a 2>/dev/null | grep -q '^discord-agent[[:space:]]'; then
    echo "$(date '+%F %T') starting existing container" >> "$LOG"
    container start discord-agent >> "$LOG" 2>&1 || "$REPO/run-container.sh" >> "$LOG" 2>&1
else
    echo "$(date '+%F %T') no container — creating fresh" >> "$LOG"
    "$REPO/run-container.sh" >> "$LOG" 2>&1
fi

echo "$(date '+%F %T') autostart done" >> "$LOG"
