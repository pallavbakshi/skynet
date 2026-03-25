FROM node:20-bookworm-slim AS agp-node


FROM python:3.12-slim AS agp-base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml /app/
COPY research/master-prd.md /app/research/master-prd.md
COPY src /app/src
COPY migrations /app/migrations
COPY scripts /app/scripts

RUN pip install ".[server]"


FROM agp-base AS agp-control-plane
COPY scripts/docker/control-plane-entrypoint.sh /usr/local/bin/agp-control-plane-entrypoint

RUN chmod +x /usr/local/bin/agp-control-plane-entrypoint

ENV AGP_HOST=0.0.0.0 \
    AGP_PORT=7860

ENTRYPOINT ["agp-control-plane-entrypoint"]


FROM agp-base AS agp-runtime
COPY --from=agp-node /usr/local/ /usr/local/
COPY scripts/docker/runtime-entrypoint.sh /usr/local/bin/agp-runtime-entrypoint

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        less \
        procps \
        ripgrep \
        sqlite3 \
        tmux=3.5a-3 \
    && npm install -g @openai/codex@0.116.0 @anthropic-ai/claude-code@2.1.81 \
    && ln -sf /usr/local/bin/codex /usr/local/bin/ncodex \
    && ln -sf /usr/local/bin/claude /usr/local/bin/claude-code \
    && chmod +x /usr/local/bin/agp-runtime-entrypoint \
    && rm -rf /var/lib/apt/lists/*

# WezTerm nightly — headless mux-server mode for wezterm-based hosts.
ARG WEZTERM_DEB_URL=https://github.com/wezterm/wezterm/releases/download/nightly/wezterm-nightly.Debian12.arm64.deb
RUN curl -fsSL "${WEZTERM_DEB_URL}" -o /tmp/wezterm.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/wezterm.deb \
    && rm -f /tmp/wezterm.deb \
    && rm -rf /var/lib/apt/lists/*

# WezTerm headless config — large terminal (200x50), 10k scrollback,
# no GUI chrome.  Placed in /etc/wezterm so it applies to all users.
COPY scripts/docker/wezterm.lua /etc/wezterm/wezterm.lua
ENV WEZTERM_CONFIG_FILE=/etc/wezterm/wezterm.lua

ENV AGP_RUNTIME_TERMINAL_HOST_KIND=tmux \
    AGP_RUNTIME_AGENT_ADAPTER_KIND=codex \
    AGP_CODEX_TUI_MODE=true \
    AGP_CODEX_CLI_COMMAND="codex -a never -s danger-full-access" \
    AGP_RUNTIME_ARTIFACT_ROOT=/artifacts \
    AGP_LOG_ROOT=/logs \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_TELEMETRY=1 \
    NO_UPDATE_NOTIFIER=1

# ── Auth & identity ──────────────────────────────────────────────────
# Auth lives in a Docker volume (agp-claude-auth), not in the image.
# Mount it at /auth — the entrypoint copies credentials into ~/.claude/.
# This means you can rebuild this image freely without losing auth.
#
# Stable machine identity so all containers look like one machine:
#   docker run --hostname agp-runtime --mac-address 02:42:37:fc:f5:93 \
#              -v agp-claude-auth:/auth ...
#
# One-time OAuth setup (only needed once, ever):
#   docker run -it --hostname agp-runtime --mac-address 02:42:37:fc:f5:93 \
#              -v agp-claude-auth:/auth <image> \
#              bash -c 'useradd -m agpuser && runuser -u agpuser -- \
#              claude --dangerously-skip-permissions'
#   (complete OAuth in browser, /exit, done — auth persists in the volume)
LABEL agp.runtime.hostname="agp-runtime" \
      agp.runtime.mac="02:42:37:fc:f5:93"

ENTRYPOINT ["agp-runtime-entrypoint"]
