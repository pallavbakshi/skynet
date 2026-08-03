#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

export SMALLOPS_CLAUDE_CODE_CORPUS_OUT="${SMALLOPS_CLAUDE_CODE_CORPUS_OUT:-/app/test-artifacts/docker-current/claude-code-corpus-candidate}"
export SMALLOPS_CODEX_CORPUS_OUT="${SMALLOPS_CODEX_CORPUS_OUT:-/app/test-artifacts/docker-current/codex-corpus-candidate}"
export SMALLOPS_DOCKER_PYTEST_ARGS="${SMALLOPS_DOCKER_PYTEST_ARGS:-smallops_tests/ -m docker -q --tb=short}"

cat <<EOF
smallops version qualification
  pytest_args=${SMALLOPS_DOCKER_PYTEST_ARGS}
  claude_corpus_out=${SMALLOPS_CLAUDE_CODE_CORPUS_OUT}
  codex_corpus_out=${SMALLOPS_CODEX_CORPUS_OUT}
EOF

"${ROOT}/scripts/docker/run-smallops-tests.sh"
