#!/usr/bin/env bash
# DEPRECATED: Use `skyops smoke` instead (validates + sends test job + checks artifacts).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

SERVER_URL="${AGP_SERVER_URL:-http://127.0.0.1:7860}"

if command -v uv >/dev/null 2>&1; then
  PYTHON_CMD=(uv run python)
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON_CMD=(.venv/bin/python)
else
  PYTHON_CMD=(python)
fi

"${PYTHON_CMD[@]}" scripts/validate_phase3_assets.py
if ! curl -fsS "${SERVER_URL}/health" >/dev/null 2>&1; then
  echo "AGP stack is not healthy at ${SERVER_URL}. Run ./scripts/phase3_stack_up.sh first." >&2
  exit 1
fi
"${PYTHON_CMD[@]}" scripts/smoke_local_stack.py
