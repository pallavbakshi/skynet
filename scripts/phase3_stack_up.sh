#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  echo "Docker daemon is not reachable" >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run python)
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON_CMD=(.venv/bin/python)
else
  PYTHON_CMD=(python)
fi

"${DOCKER[@]}" compose -f compose.phase3.yaml up -d --build
"${PYTHON_CMD[@]}" scripts/smoke_local_stack.py

echo
echo "Phase 3 stack is up."
echo "Control plane: http://127.0.0.1:7860"
echo "MinIO API:      http://127.0.0.1:9000"
echo "MinIO console:  http://127.0.0.1:9001"
