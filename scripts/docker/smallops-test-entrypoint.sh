#!/usr/bin/env bash
set -euo pipefail

SMALLOPS_USER="${SMALLOPS_DOCKER_USER:-smallops}"
SMALLOPS_HOME="${HOME:-/tmp/smallops-home}"

if ! id "${SMALLOPS_USER}" >/dev/null 2>&1; then
  useradd -m -d "${SMALLOPS_HOME}" -s /bin/bash "${SMALLOPS_USER}"
fi

mkdir -p "${SMALLOPS_HOME}"
chown "${SMALLOPS_USER}:${SMALLOPS_USER}" "${SMALLOPS_HOME}"
chmod 700 "${SMALLOPS_HOME}"
mkdir -p /tmp/pytest-cache
chown "${SMALLOPS_USER}:${SMALLOPS_USER}" /tmp/pytest-cache

if [[ -d /app/test-artifacts ]]; then
  chown -R "${SMALLOPS_USER}:${SMALLOPS_USER}" /app/test-artifacts
fi

if [[ -d /tmp ]]; then
  chmod 1777 /tmp
fi

USER_ENV=(
  HOME="${SMALLOPS_HOME}"
  PATH="/usr/local/bin:/usr/bin:/bin"
  TERM="${TERM:-xterm-256color}"
  WEZTERM_CONFIG_FILE="${WEZTERM_CONFIG_FILE:-/etc/wezterm/wezterm.lua}"
  PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
  PYTEST_ADDOPTS="${PYTEST_ADDOPTS:--o cache_dir=/tmp/pytest-cache}"
  SMALLOPS_DOCKER="${SMALLOPS_DOCKER:-1}"
  SMALLOPS_DOCKER_ARTIFACT_DIR="${SMALLOPS_DOCKER_ARTIFACT_DIR:-/app/test-artifacts/docker-current}"
  DISABLE_AUTOUPDATER="${DISABLE_AUTOUPDATER:-1}"
  DISABLE_TELEMETRY="${DISABLE_TELEMETRY:-1}"
  NO_UPDATE_NOTIFIER="${NO_UPDATE_NOTIFIER:-1}"
)

for _var in \
  ANTHROPIC_API_KEY \
  ANTHROPIC_BASE_URL \
  ANTHROPIC_AUTH_TOKEN \
  ANTHROPIC_DEFAULT_OPUS_MODEL \
  ANTHROPIC_DEFAULT_SONNET_MODEL \
  ANTHROPIC_DEFAULT_HAIKU_MODEL \
  API_TIMEOUT_MS \
  CLAUDE_CODE_SUBAGENT_MODEL \
  CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS \
  OPENAI_API_KEY \
  OPENAI_BASE_URL \
  OPENROUTER_API_KEY; do
  if [[ -v "${_var}" ]]; then
    USER_ENV+=("${_var}=${!_var}")
  fi
done

if command -v wezterm-mux-server >/dev/null 2>&1; then
  runuser -u "${SMALLOPS_USER}" -- env "${USER_ENV[@]}" wezterm-mux-server --daemonize 2>/tmp/wezterm-mux-server.log || true
fi

exec runuser -u "${SMALLOPS_USER}" -- env "${USER_ENV[@]}" python -m pytest "$@"
