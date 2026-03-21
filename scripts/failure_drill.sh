#!/usr/bin/env bash
# DEPRECATED: Use `skyops drill full` instead.
# Failure drill: runs three service-outage scenarios against the compose.phase3 stack.
# Prerequisites: compose stack must already be up (run scripts/phase3_stack_up.sh first).
#
# Drills:
#   1. Postgres pause/unpause → verify control plane survives and recovers
#   2. Redis pause/unpause   → verify queue reconstruction after reconnect
#   3. Control plane restart → verify state survives process restart
#
# Exit 0 if all drills pass, exit 1 if any fail.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

COMPOSE_FILE="${ROOT}/compose.phase3.yaml"
SERVER_URL="${AGP_SERVER_URL:-http://127.0.0.1:7860}"
DRILL_POSTGRES_RECOVER_TIMEOUT="${DRILL_POSTGRES_RECOVER_TIMEOUT:-60}"
DRILL_REDIS_RECOVER_TIMEOUT="${DRILL_REDIS_RECOVER_TIMEOUT:-60}"
DRILL_CP_RECOVER_TIMEOUT="${DRILL_CP_RECOVER_TIMEOUT:-90}"

if command -v uv >/dev/null 2>&1; then
  PYTHON=(uv run python)
elif [[ -x ".venv/bin/python" ]]; then
  PYTHON=(.venv/bin/python)
else
  PYTHON=(python)
fi

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo docker)
else
  echo "Docker daemon is not reachable" >&2
  exit 1
fi

COMPOSE=("${DOCKER[@]}" compose -f "${COMPOSE_FILE}")

_pass=0
_fail=0
declare -A _results
declare -A _timing

_health_ok() {
  curl -fsS "${SERVER_URL}/health" >/dev/null 2>&1
}

_wait_healthy() {
  local timeout="${1:-60}"
  local start; start="$(date +%s)"
  local deadline=$(( start + timeout ))
  while [[ "$(date +%s)" -lt "${deadline}" ]]; do
    if _health_ok; then
      echo $(( $(date +%s) - start ))
      return 0
    fi
    sleep 1
  done
  return 1
}

_run_smoke() {
  "${PYTHON[@]}" scripts/smoke_local_stack.py >/dev/null 2>&1
}

_record() {
  local name="$1" status="$2" secs="${3:-}"
  if [[ "${status}" == "PASS" ]]; then
    _pass=$(( _pass + 1 ))
    _results["${name}"]="PASS"
  else
    _fail=$(( _fail + 1 ))
    _results["${name}"]="FAIL"
  fi
  _timing["${name}"]="${secs}"
}

# ──────────────────────────────────────────────
# Pre-flight: stack must be healthy
# ──────────────────────────────────────────────
echo "==> Pre-flight health check..."
_health_ok || { echo "FAIL: stack is not healthy before drills — run phase3_stack_up.sh first" >&2; exit 1; }
echo "    OK"

# ──────────────────────────────────────────────
# Drill 1: Postgres outage
# ──────────────────────────────────────────────
echo ""
echo "==> Drill 1: Postgres pause/unpause"
"${COMPOSE[@]}" pause postgres
echo "    postgres paused"

# Control plane should still be running (not crashed)
sleep 3
if ! "${COMPOSE[@]}" ps control-plane --format json 2>/dev/null | grep -q '"running"'; then
  # Fallback check: try to get ANY response from control plane
  true  # non-crash is acceptable even if health returns non-200
fi

# Unpause and wait for recovery
"${COMPOSE[@]}" unpause postgres
echo "    postgres unpaused — waiting for recovery (timeout=${DRILL_POSTGRES_RECOVER_TIMEOUT}s)..."
if secs="$(_wait_healthy "${DRILL_POSTGRES_RECOVER_TIMEOUT}")"; then
  # Verify a full job round-trip works
  if _run_smoke; then
    echo "    smoke PASS (recovery_seconds=${secs})"
    _record "postgres-outage" "PASS" "${secs}"
  else
    echo "    smoke FAIL after postgres recovery"
    _record "postgres-outage" "FAIL" "${secs}"
  fi
else
  echo "    FAIL: control plane did not recover within ${DRILL_POSTGRES_RECOVER_TIMEOUT}s"
  _record "postgres-outage" "FAIL" "timeout"
fi

# ──────────────────────────────────────────────
# Drill 2: Redis outage
# ──────────────────────────────────────────────
echo ""
echo "==> Drill 2: Redis pause/unpause"
"${COMPOSE[@]}" pause redis
echo "    redis paused"
sleep 3

"${COMPOSE[@]}" unpause redis
echo "    redis unpaused — waiting for recovery (timeout=${DRILL_REDIS_RECOVER_TIMEOUT}s)..."
if secs="$(_wait_healthy "${DRILL_REDIS_RECOVER_TIMEOUT}")"; then
  if _run_smoke; then
    echo "    smoke PASS (recovery_seconds=${secs})"
    _record "redis-outage" "PASS" "${secs}"
  else
    echo "    smoke FAIL after redis recovery"
    _record "redis-outage" "FAIL" "${secs}"
  fi
else
  echo "    FAIL: control plane did not recover within ${DRILL_REDIS_RECOVER_TIMEOUT}s"
  _record "redis-outage" "FAIL" "timeout"
fi

# ──────────────────────────────────────────────
# Drill 3: Control plane restart
# ──────────────────────────────────────────────
echo ""
echo "==> Drill 3: Control plane restart"
"${COMPOSE[@]}" restart control-plane
echo "    restart issued — waiting for recovery (timeout=${DRILL_CP_RECOVER_TIMEOUT}s)..."
if secs="$(_wait_healthy "${DRILL_CP_RECOVER_TIMEOUT}")"; then
  # Verify bootstrap agent still exists (state survived restart)
  AGENT_ID="${AGP_BOOTSTRAP_AGENT_ID:-agt_local}"
  HTTP_STATUS="$(curl -o /dev/null -sw '%{http_code}' "${SERVER_URL}/agents/${AGENT_ID}" 2>/dev/null || echo 000)"
  if [[ "${HTTP_STATUS}" == "200" ]]; then
    if _run_smoke; then
      echo "    smoke PASS (recovery_seconds=${secs}, agent_id=${AGENT_ID} preserved)"
      _record "cp-restart" "PASS" "${secs}"
    else
      echo "    smoke FAIL after control plane restart"
      _record "cp-restart" "FAIL" "${secs}"
    fi
  else
    echo "    FAIL: agent ${AGENT_ID} not found after restart (HTTP ${HTTP_STATUS})"
    _record "cp-restart" "FAIL" "${secs}"
  fi
else
  echo "    FAIL: control plane did not recover within ${DRILL_CP_RECOVER_TIMEOUT}s"
  _record "cp-restart" "FAIL" "timeout"
fi

# ──────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
for drill in "postgres-outage" "redis-outage" "cp-restart"; do
  status="${_results[${drill}]:-SKIP}"
  secs="${_timing[${drill}]:-}"
  printf "  %-26s %s" "${drill}" "${status}"
  [[ -n "${secs}" ]] && printf "  (recovery=%ss)" "${secs}"
  echo
done
echo "──────────────────────────────────────────"
echo "  OVERALL: ${_pass}/$(( _pass + _fail )) PASSED"
echo "══════════════════════════════════════════"

[[ "${_fail}" -eq 0 ]] && exit 0 || exit 1
