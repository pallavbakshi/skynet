#!/usr/bin/env bash
set -euo pipefail

# ── Pass-through: run arbitrary commands instead of the work loop ────
if [[ $# -gt 0 && "${1}" != -* ]]; then
  exec "$@"
fi

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  exec python -m agp runtime-work-loop dummy "$@"
fi

# ── Required vars ────────────────────────────────────────────────────
: "${AGP_RUNTIME_ID:?AGP_RUNTIME_ID must be set}"
: "${AGP_SERVER_URL:?AGP_SERVER_URL must be set}"

# ── Runtime user setup ───────────────────────────────────────────────
# Create a non-root user for CLI tools that refuse to run as root
# (e.g. claude --dangerously-skip-permissions).
AGP_USER="${AGP_RUNTIME_USER:-agpuser}"
AGP_USER_HOME="/home/${AGP_USER}"
if ! id "${AGP_USER}" &>/dev/null; then
  useradd -m -s /bin/bash "${AGP_USER}"
fi

# ── Claude Code auth injection ───────────────────────────────────────
# Mount the agp-claude-auth volume at /auth and the entrypoint copies
# credentials + onboarding state into the user's home.
# This survives image rebuilds — OAuth is done once, persisted forever.
#
# The volume contains:
#   .credentials.json   — OAuth tokens
#   .claude.json         — onboarding state (hasCompletedOnboarding, etc.)
#   settings.json        — user preferences
#   plugins/, sessions/  — cached state
AUTH_MOUNT="${AGP_CLAUDE_AUTH_DIR:-/auth}"
if [[ -d "${AUTH_MOUNT}" && -f "${AUTH_MOUNT}/.credentials.json" ]]; then
  mkdir -p "${AGP_USER_HOME}/.claude"
  # Copy the .claude/ directory contents
  cp -a "${AUTH_MOUNT}/." "${AGP_USER_HOME}/.claude/"
  chmod 600 "${AGP_USER_HOME}/.claude/.credentials.json"
  # Copy .claude.json (TUI onboarding state) — lives in $HOME, not .claude/
  if [[ -f "${AUTH_MOUNT}/.claude.json" ]]; then
    cp "${AUTH_MOUNT}/.claude.json" "${AGP_USER_HOME}/.claude.json"
  fi
  chown -R "${AGP_USER}:${AGP_USER}" "${AGP_USER_HOME}/.claude" "${AGP_USER_HOME}/.claude.json" 2>/dev/null || true
fi

# ── Git safe directories ────────────────────────────────────────────
if command -v git >/dev/null 2>&1; then
  for path in "${AGP_TMUX_DEFAULT_CWD:-}" "${AGP_WEZTERM_DEFAULT_CWD:-}" "/workspace/main"; do
    if [[ -n "${path}" && -d "${path}" ]]; then
      git config --global --add safe.directory "${path}" >/dev/null 2>&1 || true
    fi
  done
fi

# ── App permissions ──────────────────────────────────────────────────
chmod -R a+rX /app 2>/dev/null || true

# ── Build work-loop args ─────────────────────────────────────────────
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

# ── Run as non-root user ─────────────────────────────────────────────
exec runuser -u "${AGP_USER}" -- env \
  HOME="${AGP_USER_HOME}" \
  PATH="/usr/local/bin:/usr/bin:/bin" \
  DISABLE_AUTOUPDATER="${DISABLE_AUTOUPDATER:-1}" \
  DISABLE_TELEMETRY="${DISABLE_TELEMETRY:-1}" \
  WEZTERM_CONFIG_FILE="${WEZTERM_CONFIG_FILE:-/etc/wezterm/wezterm.lua}" \
  "${ARGS[@]}"
