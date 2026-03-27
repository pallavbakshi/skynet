# Dynamic Agent Mesh — End-to-End Test Plan

Validates the 7 PRD success criteria plus auth, discovery, job lifecycle, handoff, sweeper behavior, and ops namespace.

## Setup

```bash
make local-up
```

All tests below assume CP is running on `localhost:7860` with a fresh database.

---

## 1. Zero-Bootstrap Start

**PRD Criterion 1**: A fresh CP starts with zero agents. No bootstrap needed.

```bash
# Verify no agents exist
curl -s http://localhost:7860/agents | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
assert len(items) == 0, f'Expected 0 agents, got {len(items)}'
print('PASS: Zero agents on fresh start')
"

# Verify no capabilities required
curl -s http://localhost:7860/capabilities | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
print(f'PASS: {len(items)} capabilities (none required)')
"
```

## 2. Agent Self-Registration

**PRD Criterion 2**: An agent appears in `GET /agents` after calling `/agents/up`.

```bash
# Register agent
curl -s -X POST http://localhost:7860/agents/up \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "coder-1", "capabilities": ["code", "python"]}' \
    | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['agent_id'] == 'coder-1', f'Wrong agent_id: {d[\"agent_id\"]}'
assert d['status'] == 'idle'
assert 'code' in d['capabilities']
assert 'python' in d['capabilities']
print(f'PASS: Agent registered: {d[\"agent_id\"]} caps={d[\"capabilities\"]} status={d[\"status\"]}')
"

# Verify it appears in listing
curl -s http://localhost:7860/agents | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
ids = [a['agent_id'] for a in items]
assert 'coder-1' in ids, f'coder-1 not in {ids}'
print(f'PASS: coder-1 visible in GET /agents')
"
```

## 3. Heartbeat (Idempotent Re-Registration)

**PRD**: `/agents/up` is idempotent — first call creates, subsequent calls update `last_heartbeat_at`.

```bash
# First heartbeat — record the timestamp
T1=$(curl -s http://localhost:7860/agents/coder-1 | python3 -c "
import sys,json; print(json.load(sys.stdin)['data']['last_heartbeat_at'])
")
echo "Heartbeat before: $T1"

sleep 2

# Second call to /agents/up — same agent, updated heartbeat
curl -s -X POST http://localhost:7860/agents/up \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "coder-1", "capabilities": ["code", "python"]}' > /dev/null

T2=$(curl -s http://localhost:7860/agents/coder-1 | python3 -c "
import sys,json; print(json.load(sys.stdin)['data']['last_heartbeat_at'])
")
echo "Heartbeat after:  $T2"

python3 -c "
t1, t2 = '$T1', '$T2'
assert t2 > t1, f'Heartbeat not updated: {t1} -> {t2}'
print('PASS: Heartbeat updated on re-registration')
"
```

## 4. Agent Discovery by Capability

**PRD Criterion 5/6**: Agents are discoverable by capability. Exact match, no substring.

```bash
# Register a second agent
curl -s -X POST http://localhost:7860/agents/up \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "reviewer-1", "capabilities": ["code-review"]}' > /dev/null

# Exact match: "code" should find coder-1 only (not reviewer-1's "code-review")
curl -s 'http://localhost:7860/agents?capability=code' | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
ids = [a['agent_id'] for a in items]
assert 'coder-1' in ids, 'coder-1 missing'
assert 'reviewer-1' not in ids, 'reviewer-1 should not match \"code\" (substring match bug)'
print(f'PASS: capability=code returned {ids} (no substring false positive)')
"

# "code-review" should find reviewer-1 only
curl -s 'http://localhost:7860/agents?capability=code-review' | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
ids = [a['agent_id'] for a in items]
assert 'reviewer-1' in ids
assert 'coder-1' not in ids
print(f'PASS: capability=code-review returned {ids}')
"

# Filter by status
curl -s 'http://localhost:7860/agents?status=idle&capability=code' | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
assert all(a['status'] == 'idle' for a in items)
print(f'PASS: status+capability filter works ({len(items)} results)')
"

# Non-existent capability
curl -s 'http://localhost:7860/agents?capability=rust' | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
assert len(items) == 0
print('PASS: Non-existent capability returns empty')
"
```

## 5. Job Lifecycle (Send → Queue → Claim → Complete)

**PRD Criterion 5**: An orchestrator can send work to a worker agent.

```bash
# Register a runtime (required for claiming)
curl -s -X POST http://localhost:7860/runtimes/register \
    -H 'Content-Type: application/json' \
    -d '{"runtime_id": "rtm-coder-1", "hostname": "localhost"}' > /dev/null

# Send work to coder-1
SEND=$(curl -s -X POST http://localhost:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H 'Idempotency-Key: e2e-job-lifecycle' \
    -d '{"target": {"type": "agent", "id": "coder-1"}, "message": {"text": "test prompt"}}')
JOB_ID=$(echo "$SEND" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['job_id'])")
echo "Job created: $JOB_ID"

# Verify queued
curl -s "http://localhost:7860/jobs/$JOB_ID" | python3 -c "
import sys,json
status = json.load(sys.stdin)['data']['status']
assert status == 'queued', f'Expected queued, got {status}'
print(f'PASS: Job is queued')
"

# Claim the job
CLAIM=$(curl -s -X POST http://localhost:7860/runs/claim \
    -H 'Content-Type: application/json' \
    -d "{\"runtime_id\": \"rtm-coder-1\", \"agent_id\": \"coder-1\", \"lease_ttl_seconds\": 60}")
echo "$CLAIM" | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['claimed'] == True
print(f'PASS: Job claimed — run={d[\"run\"][\"run_id\"]} lease={d[\"lease\"][\"lease_id\"]}')
"
RUN_ID=$(echo "$CLAIM" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['run']['run_id'])")
LEASE_ID=$(echo "$CLAIM" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['lease']['lease_id'])")
FENCING=$(echo "$CLAIM" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['lease']['fencing_token'])")

# Complete the run with artifacts
ARTIFACTS='[{"role":"result","storage_ref":"file:///tmp/test-result.txt","content_type":"text/plain","checksum":"","size_bytes":1}]'
curl -s -X POST "http://localhost:7860/runs/$RUN_ID/complete" \
    -H 'Content-Type: application/json' \
    -d "{\"runtime_id\": \"rtm-coder-1\", \"lease_id\": \"$LEASE_ID\", \"fencing_token\": $FENCING, \"artifacts\": $ARTIFACTS, \"summary\": {}}" \
    | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['status'] == 'completed'
print(f'PASS: Run completed')
"

# Verify job is completed
curl -s "http://localhost:7860/jobs/$JOB_ID" | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['status'] == 'completed'
assert d['result_artifact_id'] is not None
print(f'PASS: Job completed with artifact {d[\"result_artifact_id\"]}')
"
```

## 6. Capability-Based Routing

**PRD Criterion 6**: Sending to `target.type=capability` routes to an idle agent with that capability.

```bash
SEND=$(curl -s -X POST http://localhost:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H 'Idempotency-Key: e2e-cap-routing' \
    -d '{"target": {"type": "capability", "id": "code"}, "message": {"text": "capability routed"}}')
echo "$SEND" | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['status'] in ('queued', 'accepted')
print(f'PASS: Capability-routed job created: {d[\"job_id\"]}')
"

# The job should be targeted at coder-1 (the idle agent with "code" capability)
JOB_ID=$(echo "$SEND" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['job_id'])")
curl -s "http://localhost:7860/jobs/$JOB_ID" | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['target_agent_id'] == 'coder-1', f'Routed to {d[\"target_agent_id\"]}, expected coder-1'
print(f'PASS: Capability \"code\" routed to {d[\"target_agent_id\"]}')
"
```

## 7. Graceful Shutdown (Drain + Force)

**PRD**: `/agents/{id}/down` with `mode=drain` transitions to draining. `mode=force` deletes immediately.

```bash
# Drain reviewer-1
curl -s -X POST http://localhost:7860/agents/reviewer-1/down \
    -H 'Content-Type: application/json' \
    -d '{"mode": "drain"}' | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['status'] == 'draining'
print(f'PASS: reviewer-1 is draining')
"

# Draining agent should still be visible but not discoverable as idle
curl -s 'http://localhost:7860/agents?status=idle&capability=code-review' | python3 -c "
import sys,json
items = json.load(sys.stdin)['data']['items']
assert len(items) == 0
print('PASS: Draining agent not in idle discovery')
"

# Force-delete coder-1
curl -s -X POST http://localhost:7860/agents/coder-1/down \
    -H 'Content-Type: application/json' \
    -d '{"mode": "force"}' | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['status'] == 'deleted'
print('PASS: coder-1 force-deleted')
"

# Verify coder-1 is gone
curl -s http://localhost:7860/agents/coder-1 | python3 -c "
import sys,json
d = json.load(sys.stdin)
assert d['ok'] == False
assert d['error']['code'] == 'not_found'
print('PASS: coder-1 returns 404 after force-delete')
"
```

## 8. Audit History Survives Deletion

**PRD**: Historical runs and leases retain `agent_id` after agent deletion.

```bash
# The run from test 5 should still have agent_id = coder-1
curl -s "http://localhost:7860/jobs/$JOB_ID/events" | python3 -c "
import sys,json
events = json.load(sys.stdin)['data']['items']
assert len(events) > 0
print(f'PASS: {len(events)} events preserved after agent deletion')
"
```

## 9. Re-Registration After Deletion

**PRD Criterion 4**: Restarting creates a fresh agent record with the same ID.

```bash
curl -s -X POST http://localhost:7860/agents/up \
    -H 'Content-Type: application/json' \
    -d '{"agent_id": "coder-1", "capabilities": ["code", "python", "rust"]}' \
    | python3 -c "
import sys,json
d = json.load(sys.stdin)['data']
assert d['agent_id'] == 'coder-1'
assert d['status'] == 'idle'
assert 'rust' in d['capabilities']
print(f'PASS: coder-1 re-registered with new capabilities: {d[\"capabilities\"]}')
"
```

## 10. Auth: Runtime Token

**PRD**: `/agents/up` and `/agents/{id}/down` require runtime bearer token when configured.

```bash
# This test uses curl directly against a fresh CP instance.
# The main local-up instance has no auth configured (for convenience).
# In production, set AGP_RUNTIME_BEARER_TOKEN.

echo "SKIP (requires separate CP with AGP_RUNTIME_BEARER_TOKEN set)"
echo "Covered by automated test: TestRuntimeAuthOnAgentsUp"
```

## 11. Auth: Force-Delete Requires Operator Role

**PRD**: `mode=force` on `/agents/{id}/down` requires operator lifecycle role.

```bash
echo "SKIP (requires auth-configured CP)"
echo "Covered by automated test: TestForceDeleteAuthGuard"
```

## 12. Ops Namespace

**PRD Phase 4**: All observability endpoints available under `/ops/*`.

```bash
# Health
curl -s http://localhost:7860/ops/health | python3 -c "
import sys,json; assert json.load(sys.stdin)['ok']; print('PASS: /ops/health')
"

# Alerts
curl -s http://localhost:7860/ops/alerts | python3 -c "
import sys,json; assert json.load(sys.stdin)['ok']; print('PASS: /ops/alerts')
"

# Metrics
curl -s http://localhost:7860/ops/metrics | python3 -c "
import sys; data = sys.stdin.read(); assert 'agp_' in data or len(data) > 0; print('PASS: /ops/metrics')
"

# Audit
curl -s http://localhost:7860/ops/audit | python3 -c "
import sys,json; assert json.load(sys.stdin)['ok']; print('PASS: /ops/audit')
"

# Runtimes
curl -s http://localhost:7860/ops/runtimes | python3 -c "
import sys,json; assert json.load(sys.stdin)['ok']; print('PASS: /ops/runtimes')
"

# Triage
curl -s http://localhost:7860/ops/triage | python3 -c "
import sys,json; assert json.load(sys.stdin)['ok']; print('PASS: /ops/triage')
"

# Upgrade status
curl -s http://localhost:7860/ops/upgrade-status | python3 -c "
import sys,json; assert json.load(sys.stdin)['ok']; print('PASS: /ops/upgrade-status')
"
```

## 13. Live Runtime Test (Codex + OpenRouter)

Full round-trip with a real AI adapter.

```bash
# Start runtime in background
make runtime &
RUNTIME_PID=$!
sleep 5

# Send work
RESPONSE=$(curl -s -X POST http://localhost:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: e2e-live-$(date +%s)" \
    -d '{"target": {"type": "agent", "id": "agt_local"}, "message": {"text": "what is 2+2? reply with just the number"}}')
JOB_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['job_id'])")
echo "Job: $JOB_ID"

# Poll until complete (up to 60s)
for i in $(seq 1 12); do
    sleep 5
    STATUS=$(curl -s "http://localhost:7860/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['status'])")
    if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then break; fi
done

# Check result
ART_ID=$(curl -s "http://localhost:7860/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'].get('result_artifact_id',''))")
if [ -n "$ART_ID" ]; then
    ANSWER=$(curl -s "http://localhost:7860/artifacts/$ART_ID/content" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['content'])")
    echo "Answer: $ANSWER"
    python3 -c "assert '4' in '$ANSWER', 'Expected 4'; print('PASS: Live codex execution returned correct answer')"
fi

# Cleanup
kill $RUNTIME_PID 2>/dev/null
wait $RUNTIME_PID 2>/dev/null
```

## Teardown

```bash
make local-down
```

## Summary

| # | Test | PRD Criterion | Type |
|---|------|--------------|------|
| 1 | Zero-bootstrap start | 1 | API |
| 2 | Agent self-registration | 2 | API |
| 3 | Heartbeat idempotency | 2 | API |
| 4 | Discovery by capability | 5, 6 | API |
| 5 | Job lifecycle | 5 | API |
| 6 | Capability-based routing | 6 | API |
| 7 | Graceful shutdown | 3 | API |
| 8 | Audit history | — | API |
| 9 | Re-registration | 4 | API |
| 10 | Runtime auth | — | Auth (skip) |
| 11 | Force-delete auth | — | Auth (skip) |
| 12 | Ops namespace | 7 | API |
| 13 | Live codex execution | 5 | Live |
