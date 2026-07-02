FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISABLE_AUTOUPDATER=1

WORKDIR /app

COPY requirements.txt /app/

ARG CLAUDE_CODE_VERSION=latest

# System tooling the agent actually needs to do dev/pipeline work:
#   ffmpeg            — remux/transcode (the video-capture fix needs this)
#   postgresql-client — psql against SUPABASE_DB_URL
#   libpq-dev + build-essential — so `pip install psycopg2` etc. work in a venv
#   gh                — open/merge PRs
#   curl, jq, unzip   — general scripting
#   tmux              — long-lived sessions that outlive a single @-invocation;
#                       used to spawn/maintain sub-agents (see subagent.py +
#                       the `subagents` skill)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs npm ca-certificates git curl jq unzip sudo openssh-client \
        ffmpeg postgresql-client libpq-dev build-essential tmux \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version

RUN useradd --create-home --uid 10001 agent

# Let the agent install whatever a task needs (apt/pip/npm) without a rebuild.
# Passwordless sudo is acceptable because the container is the isolation
# boundary; root inside it is still confined to the container.
RUN echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# Default git identity (for commits/PRs) and trust the bind-mounted /app even
# though its files are owned by the host uid.
RUN git config --system user.name "CowBot" \
    && git config --system user.email "cowbot@users.noreply.github.com" \
    && git config --system safe.directory '*'

# Python runtime lives in src/; CLAUDE.md stays at the repo root (claude's cwd).
# (In the container deployment the host repo is bind-mounted over /app, so this
# COPY is mainly for running the image standalone.)
COPY --chown=agent:agent src/ /app/src/
COPY --chown=agent:agent CLAUDE.md /app/

USER agent

CMD ["python", "src/discord_agent_runtime.py"]
