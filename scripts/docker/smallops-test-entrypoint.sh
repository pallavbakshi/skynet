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

if [[ -z "${SMALLOPS_CODEX_OPENROUTER_API_KEY:-}" ]]; then
  if [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" && "${ANTHROPIC_BASE_URL:-}" == *"openrouter.ai"* ]]; then
    export SMALLOPS_CODEX_OPENROUTER_API_KEY="${ANTHROPIC_AUTH_TOKEN}"
  else
    export SMALLOPS_CODEX_OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-${ANTHROPIC_AUTH_TOKEN:-}}"
  fi
fi
if [[ -n "${SMALLOPS_CODEX_OPENROUTER_API_KEY:-}" ]]; then
  export OPENAI_API_KEY=""
fi

CODEX_MODEL="${SMALLOPS_CODEX_MODEL:-${AGP_CODEX_MODEL:-openai/gpt-5.3-codex}}"
CODEX_CONFIG_HOME="${SMALLOPS_HOME}/.codex"
mkdir -p "${CODEX_CONFIG_HOME}" "${SMALLOPS_HOME}/.config/codex"
CODEX_MODEL_TEMPLATE="${SMALLOPS_CODEX_MODEL_TEMPLATE:-gpt-5.4}"
HOME="${SMALLOPS_HOME}" codex debug models --bundled 2>/tmp/smallops-codex-model-catalog.err \
  | jq --arg model "${CODEX_MODEL}" --arg template "${CODEX_MODEL_TEMPLATE}" '{
      models: [
        .models[]
        | select(.slug == $template)
        | .slug = $model
        | .display_name = $model
        | .description = "smallops Docker model catalog override for OpenRouter Codex tests."
        | .default_reasoning_level = "low"
        | .additional_speed_tiers = []
        | .service_tiers = []
      ]
    }' > "${CODEX_CONFIG_HOME}/smallops-model-catalog.json"
if [[ ! -s "${CODEX_CONFIG_HOME}/smallops-model-catalog.json" ]]; then
  cat /tmp/smallops-codex-model-catalog.err >&2
  echo "failed to generate Codex model catalog for ${CODEX_MODEL} from ${CODEX_MODEL_TEMPLATE}" >&2
  exit 1
fi
cat > "${CODEX_CONFIG_HOME}/config.toml" <<EOF
model = "${CODEX_MODEL}"
model_provider = "openrouter"
model_reasoning_effort = "low"
model_catalog_json = "${CODEX_CONFIG_HOME}/smallops-model-catalog.json"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
wire_api = "responses"

[model_providers.openrouter.auth]
command = "sh"
args = ["-c", "echo \$SMALLOPS_CODEX_OPENROUTER_API_KEY"]

[projects."/app"]
trust_level = "trusted"

[projects."/tmp"]
trust_level = "trusted"
EOF
cat > "${CODEX_CONFIG_HOME}/openrouter.config.toml" <<EOF
model = "${CODEX_MODEL}"
model_provider = "openrouter"
model_reasoning_effort = "low"
model_catalog_json = "${CODEX_CONFIG_HOME}/smallops-model-catalog.json"
EOF
cp "${CODEX_CONFIG_HOME}/config.toml" "${SMALLOPS_HOME}/.config/codex/config.toml"
cp "${CODEX_CONFIG_HOME}/openrouter.config.toml" "${SMALLOPS_HOME}/.config/codex/openrouter.config.toml"
cp "${CODEX_CONFIG_HOME}/smallops-model-catalog.json" "${SMALLOPS_HOME}/.config/codex/smallops-model-catalog.json"
chown -R "${SMALLOPS_USER}:${SMALLOPS_USER}" "${CODEX_CONFIG_HOME}" "${SMALLOPS_HOME}/.config"

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
  OPENROUTER_API_KEY \
  SMALLOPS_CODEX_OPENROUTER_API_KEY \
  SMALLOPS_CODEX_MODEL \
  SMALLOPS_CODEX_CORPUS_OUT \
  AGP_CODEX_MODEL; do
  if [[ -v "${_var}" ]]; then
    USER_ENV+=("${_var}=${!_var}")
  fi
done

if command -v wezterm-mux-server >/dev/null 2>&1; then
  runuser -u "${SMALLOPS_USER}" -- env "${USER_ENV[@]}" wezterm-mux-server --daemonize 2>/tmp/wezterm-mux-server.log || true
fi

exec runuser -u "${SMALLOPS_USER}" -- env "${USER_ENV[@]}" python -m pytest "$@"
