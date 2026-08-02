#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${SMALLOPS_DOCKER_IMAGE:-agp-smallops-test:latest}"
ENV_FILE="${SMALLOPS_DOCKER_ENV_FILE:-${ROOT}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

if [[ -n "${SMALLOPS_DOCKER_PYTEST_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  PYTEST_ARGS=(${SMALLOPS_DOCKER_PYTEST_ARGS})
else
  PYTEST_ARGS=(smallops_tests/ -m docker -q --tb=short)
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed or not in PATH" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is not reachable" >&2
  exit 1
fi

if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
  echo "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required for smallops Docker Claude Code tests" >&2
  exit 1
fi

if [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" && "${ANTHROPIC_BASE_URL:-}" == *"openrouter.ai"* ]]; then
  ANTHROPIC_API_KEY=""
fi

ARTIFACT_DIR="${ROOT}/test-artifacts/docker/$(date -u +%Y%m%d-%H%M%S)"
mkdir -p "${ARTIFACT_DIR}"

{
  echo "image=${IMAGE}"
  echo "env_file=${ENV_FILE}"
  echo "pytest_args=${PYTEST_ARGS[*]}"
  echo "credential_mode=$([[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]] && echo auth-token || echo api-key)"
  echo "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL:-}"
  echo "ANTHROPIC_DEFAULT_OPUS_MODEL=${ANTHROPIC_DEFAULT_OPUS_MODEL:-}"
  echo "ANTHROPIC_DEFAULT_SONNET_MODEL=${ANTHROPIC_DEFAULT_SONNET_MODEL:-}"
  echo "ANTHROPIC_DEFAULT_HAIKU_MODEL=${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}"
  echo "API_TIMEOUT_MS=${API_TIMEOUT_MS:-}"
  docker --version
  docker info --format 'server={{.ServerVersion}} os={{.OperatingSystem}} arch={{.Architecture}}'
} | tee "${ARTIFACT_DIR}/host-env.txt"

docker build --target smallops-test -t "${IMAGE}" "${ROOT}" 2>&1 | tee "${ARTIFACT_DIR}/docker-build.log"

docker_env=(
  -e SMALLOPS_DOCKER=1 \
  -e SMALLOPS_DOCKER_ARTIFACT_DIR=/app/test-artifacts/docker-current \
)

for var in \
  ANTHROPIC_API_KEY \
  ANTHROPIC_BASE_URL \
  ANTHROPIC_AUTH_TOKEN \
  ANTHROPIC_DEFAULT_OPUS_MODEL \
  ANTHROPIC_DEFAULT_SONNET_MODEL \
  ANTHROPIC_DEFAULT_HAIKU_MODEL \
  API_TIMEOUT_MS \
  CLAUDE_CODE_SUBAGENT_MODEL \
  CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS; do
  if [[ -v "${var}" ]]; then
    docker_env+=(-e "${var}")
  fi
done

set +e
docker run --rm \
  "${docker_env[@]}" \
  -v "${ARTIFACT_DIR}:/app/test-artifacts/docker-current" \
  "${IMAGE}" "${PYTEST_ARGS[@]}" 2>&1 | tee "${ARTIFACT_DIR}/docker-run.log"
status=${PIPESTATUS[0]}
set -e

echo "Docker smallops artifacts: ${ARTIFACT_DIR}"
exit "${status}"
