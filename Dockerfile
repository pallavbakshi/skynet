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
        tmux \
    && npm install -g @openai/codex @anthropic-ai/claude-code \
    && ln -sf /usr/local/bin/codex /usr/local/bin/ncodex \
    && ln -sf /usr/local/bin/claude /usr/local/bin/claude-code \
    && chmod +x /usr/local/bin/agp-runtime-entrypoint \
    && rm -rf /var/lib/apt/lists/*

ENV AGP_RUNTIME_TERMINAL_HOST_KIND=tmux \
    AGP_RUNTIME_AGENT_ADAPTER_KIND=codex \
    AGP_CODEX_TUI_MODE=true \
    AGP_CODEX_CLI_COMMAND="codex -a never -s danger-full-access" \
    AGP_RUNTIME_ARTIFACT_ROOT=/artifacts \
    AGP_LOG_ROOT=/logs

ENTRYPOINT ["agp-runtime-entrypoint"]
