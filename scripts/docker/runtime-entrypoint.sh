#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 0 && "${1}" != -* ]]; then
  exec "$@"
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  exec python -m agp runtime-work-loop dummy "$@"
fi

: "${AGP_RUNTIME_ID:?AGP_RUNTIME_ID must be set}"
: "${AGP_SERVER_URL:?AGP_SERVER_URL must be set}"

if command -v git >/dev/null 2>&1; then
  for path in "${AGP_TMUX_DEFAULT_CWD:-}" "${AGP_WEZTERM_DEFAULT_CWD:-}" "/workspace/main"; do
    if [[ -n "${path}" && -d "${path}" ]]; then
      git config --global --add safe.directory "${path}" >/dev/null 2>&1 || true
    fi
  done
fi

HOST_KIND="${AGP_RUNTIME_TERMINAL_HOST_KIND:-tmux}"
ADAPTER_KIND="${AGP_RUNTIME_AGENT_ADAPTER_KIND:-codex}"
HOSTNAME_ARG="${AGP_RUNTIME_HOSTNAME:-$(hostname)}"
ARTIFACT_ROOT="${AGP_RUNTIME_ARTIFACT_ROOT:-/artifacts}"
IDLE_SLEEP_SECONDS="${AGP_RUNTIME_IDLE_SLEEP_SECONDS:-1}"
MAX_LOCAL_RECOVERIES="${AGP_RUNTIME_MAX_LOCAL_RECOVERIES:-1}"

ARGS=(
  python -m agp runtime-work-loop "${AGP_RUNTIME_ID}"
  --server-url "${AGP_SERVER_URL}"
  --hostname "${HOSTNAME_ARG}"
  --host-kind "${HOST_KIND}"
  --adapter-kind "${ADAPTER_KIND}"
  --artifact-root "${ARTIFACT_ROOT}"
  --idle-sleep-seconds "${IDLE_SLEEP_SECONDS}"
  --max-local-recoveries "${MAX_LOCAL_RECOVERIES}"
)

if [[ -n "${AGP_RUNTIME_AGENT_ID:-}" ]]; then
  ARGS+=(--agent-id "${AGP_RUNTIME_AGENT_ID}")
fi

if [[ -n "${AGP_RUNTIME_CAPABILITY_ID:-}" ]]; then
  ARGS+=(--capability-id "${AGP_RUNTIME_CAPABILITY_ID}")
fi

exec "${ARGS[@]}"
