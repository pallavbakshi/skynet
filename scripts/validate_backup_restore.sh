#!/usr/bin/env bash
# Validates the full backup → restore → smoke cycle against the compose.phase3 stack.
# Prerequisites: compose stack must be up (run scripts/phase3_stack_up.sh first).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

BACKUP_DIR="${BACKUP_DIR:-/tmp/agp-br-validate-$$}"
SERVER_URL="${AGP_SERVER_URL:-http://127.0.0.1:7860}"

if command -v uv >/dev/null 2>&1; then
  PYTHON=(uv run python)
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON=(.venv/bin/python)
else
  PYTHON=(python)
fi

cleanup() {
  rm -rf "${BACKUP_DIR}"
}
trap cleanup EXIT

# Step 1: assert stack is healthy
echo "==> Checking control plane health..."
for _ in {1..30}; do
  if curl -fsS "${SERVER_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS "${SERVER_URL}/health" >/dev/null || { echo "FAIL: control plane not healthy" >&2; exit 1; }
echo "    OK"

# Step 2: create backup
echo "==> Creating backup to ${BACKUP_DIR}..."
"${PYTHON[@]}" scripts/phase3_backup_create.py "${BACKUP_DIR}"

# Step 3: validate manifest
MANIFEST="${BACKUP_DIR}/manifest.json"
[[ -f "${MANIFEST}" ]] || { echo "FAIL: manifest.json missing" >&2; exit 1; }
OBJECT_COUNT="$("${PYTHON[@]}" -c "import json,sys; m=json.load(open('${MANIFEST}')); sys.exit(0 if m['object_count'] >= 0 else 1); print(m['object_count'])")"
SQL_SIZE="$(wc -c < "${BACKUP_DIR}/postgres.sql")"
[[ "${SQL_SIZE}" -gt 100 ]] || { echo "FAIL: postgres.sql is too small (${SQL_SIZE} bytes)" >&2; exit 1; }
echo "    manifest OK (objects=${OBJECT_COUNT}, sql_bytes=${SQL_SIZE})"

# Step 4: restore (tears down stack, restores, brings back up)
echo "==> Restoring from backup..."
"${PYTHON[@]}" scripts/phase3_backup_restore.py "${BACKUP_DIR}"

# Step 5: run smoke against restored stack
echo "==> Running smoke test against restored stack..."
"${PYTHON[@]}" scripts/smoke_local_stack.py

echo
echo "backup-restore validation PASSED"
echo "  backup_dir: ${BACKUP_DIR}"
echo "  sql_bytes: ${SQL_SIZE}"
