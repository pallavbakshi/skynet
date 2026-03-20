# AGP Control Plane API Specification

## Status
Authoritative

## Purpose
This document defines the control plane API contract.

It is the source of truth for:
- endpoint behavior
- request/response shapes
- idempotency
- error model
- pagination and ordering rules

## General Rules
- All JSON requests and responses use UTF-8.
- All write endpoints support `Idempotency-Key`.
- All timestamps are ISO-8601 UTC strings.
- All list endpoints return stable ordering and cursor-based pagination.
- All event feeds are ordered by control-plane event sequence.

## Authentication Contract
- Runtime endpoints require authenticated runtime identity.
- Orchestration and operator endpoints require authenticated user or service identity.
- V1 uses bearer-token authentication only.
- Clients must send `Authorization: Bearer <token>`.
- Runtime write surfaces must use the configured runtime token.
- Orchestration/operator surfaces must use the configured operator token.
- `/health` is intentionally unauthenticated.
- mTLS or other transport identities are out of scope for V1 and may be added in a later revision.

## Idempotency Contract
- The control plane must persist idempotency records for write endpoints that accept `Idempotency-Key`.
- Reuse of the same key on the same endpoint with the same normalized request body must replay the original response.
- Reuse of the same key on the same endpoint with a different normalized request body must return `409 conflict`.
- Idempotency records must be retained long enough to cover expected client retry windows.

## Response Envelope

### Success
```json
{
  "ok": true,
  "data": {}
}
```

### Error
```json
{
  "ok": false,
  "error": {
    "code": "string",
    "message": "string",
    "retryable": false
  }
}
```

## Error Codes
- `invalid_request`
- `not_found`
- `conflict`
- `stale_fencing_token`
- `lease_expired`
- `already_terminal`
- `not_eligible`
- `unauthenticated`
- `forbidden`
- `rate_limited`
- `internal_error`

## Orchestration Endpoints

### POST `/messages/send`
Send work to a logical agent or capability target.

#### Request
```json
{
  "target": {
    "type": "agent",
    "id": "agt_123"
  },
  "message": {
    "text": "Review this diff",
    "metadata": {}
  },
  "detach_policy": {
    "mode": "auto"
  }
}
```

`target.type` may be:
- `agent`
- `capability`

#### Response: inline result
```json
{
  "ok": true,
  "data": {
    "kind": "inline_result",
    "job_id": "job_123",
    "result_artifact_id": "art_123",
    "status": "completed"
  }
}
```

#### Response: accepted async
```json
{
  "ok": true,
  "data": {
    "kind": "accepted_async",
    "job_id": "job_123",
    "status": "queued"
  }
}
```

### GET `/jobs/{job_id}`
Return job state and run summary.

#### Response
```json
{
  "ok": true,
  "data": {
    "job_id": "job_123",
    "status": "running",
    "target_agent_id": "agt_123",
    "retry_count": 1,
    "max_retries": 3,
    "latest_run": {
      "run_id": "run_123",
      "status": "running",
      "attempt": 2
    },
    "result_artifact_id": null
  }
}
```

### GET `/jobs`
List jobs with filtering and pagination.

#### Query Parameters
- `status`
- `target_agent_id`
- `created_after`
- `cursor`
- `limit`

### GET `/jobs/{job_id}/events`
Return event history for a job ordered by event sequence.

### POST `/jobs/{job_id}/interrupt`
Request cancellation of queued or running work.

#### Response
```json
{
  "ok": true,
  "data": {
    "job_id": "job_123",
    "status": "cancelled"
  }
}
```

If the job was queued, the response status is `cancelled`.
If the job was running, the response status is `interrupt_requested`.

### POST `/jobs/{job_id}/block`
Move a queued job into `blocked`.

#### Query Parameters
- `reason`

#### Response
```json
{
  "ok": true,
  "data": {
    "job_id": "job_123",
    "status": "blocked",
    "reason": "waiting_on_dependency"
  }
}
```

### POST `/jobs/{job_id}/unblock`
Move a blocked job back to `queued`.

#### Query Parameters
- `reason`

#### Response
```json
{
  "ok": true,
  "data": {
    "job_id": "job_123",
    "status": "queued",
    "reason": "dependency_resolved"
  }
}
```

### POST `/jobs/{job_id}/handoff`
Create follow-on jobs from source artifacts.

#### Request
```json
{
  "artifact_ids": ["art_123"],
  "targets": [
    {
      "type": "agent",
      "id": "agt_456"
    }
  ],
  "message": {
    "text": "Continue from this result"
  }
}
```

#### Response
```json
{
  "ok": true,
  "data": {
    "handoff_id": "hnd_123",
    "created_job_ids": ["job_456"]
  }
}
```

## Agent Endpoints

### POST `/agents/up`
Instantiate a durable agent from a capability blueprint.

#### Request
```json
{
  "capability_id": "cap_python_tester",
  "agent_id": "wrt-01",
  "workspace_ref": "/tmp/agt-wrt-01",
  "assigned_runtime_id": "rt_123"
}
```

### POST `/agents/{agent_id}/down`
Destroy a durable agent.

#### Request
```json
{
  "mode": "drain"
}
```

#### Query Parameters
- `mode=graceful|force`

### GET `/agents`
List durable agents.

### GET `/capabilities`
List capability blueprints.

## Runtime Endpoints

### GET `/system/upgrade-status`
Report the persisted control-plane release/schema state and rollback target.

### POST `/runtimes/register`
Register or refresh runtime identity.

#### Request
```json
{
  "runtime_id": "rt_123",
  "hostname": "node-1",
  "release_version": "0.1.0",
  "metadata": {}
}
```

#### Rules
- runtime registration must reject unsupported version skew
- runtime release may not be ahead of the active control-plane release
- control plane may be at most one minor version ahead of the runtime
- major-version skew is unsupported

### GET `/runtimes`
List runtimes with status and health.

### POST `/runs/claim`
Pull-based claim path.

#### Request
```json
{
  "runtime_id": "rt_123",
  "hosted_agent_ids": ["agt_123"],
  "eligible_capability_ids": ["cap_python_tester"]
}
```

#### Response
```json
{
  "ok": true,
  "data": {
    "job": {
      "job_id": "job_123"
    },
    "run": {
      "run_id": "run_123",
      "attempt": 1
    },
    "agent": {
      "agent_id": "agt_123"
    },
    "lease": {
      "lease_id": "lse_123",
      "expires_at": "2026-03-20T00:00:00Z"
    },
    "fencing_token": 17,
    "artifact_upload_policy": {
      "required_roles": ["prompt", "transcript_log", "exec_log"],
      "allow_additional_roles": true
    }
  }
}
```

### POST `/runs/{run_id}/heartbeat`

#### Request
```json
{
  "lease_id": "lse_123",
  "fencing_token": 17,
  "started": false
}
```

#### Request
```json
{
  "lease_id": "lse_123",
  "fencing_token": 17
}
```

### POST `/runs/{run_id}/progress`

#### Request
```json
{
  "lease_id": "lse_123",
  "fencing_token": 17,
  "message": "executing tests",
  "payload": {}
}
```

### POST `/runs/{run_id}/complete`

#### Request
```json
{
  "lease_id": "lse_123",
  "fencing_token": 17,
  "artifacts": [
    {"artifact_id": "art_result_123", "role": "result"},
    {"artifact_id": "art_exec_123", "role": "exec_log"},
    {"artifact_id": "art_tr_123", "role": "transcript_log"}
  ]
}
```

If the exact same terminal request is replayed under the same valid lease and fencing token, the control plane may return the already-committed terminal success response.
If the replay conflicts with committed terminal state, the control plane must return `409 conflict`.

### POST `/runs/{run_id}/fail`

#### Request
```json
{
  "lease_id": "lse_123",
  "fencing_token": 17,
  "artifacts": [
    {"artifact_id": "art_failure_123", "role": "failure_evidence"},
    {"artifact_id": "art_exec_123", "role": "exec_log"},
    {"artifact_id": "art_tr_123", "role": "transcript_log"}
  ]
}
```

If the exact same terminal request is replayed under the same valid lease and fencing token, the control plane may return the already-committed terminal failure response.
If the replay conflicts with committed terminal state, the control plane must return `409 conflict`.

## Artifact Endpoints

### GET `/artifacts/{artifact_id}`
Return artifact metadata.

### GET `/artifacts/{artifact_id}/content`
Return artifact content or retrieval location.

#### Query Parameters
- `cursor`
- `limit`

## Health Endpoint

### GET `/health`
Return overall platform health summary.

## Idempotency Rules
- `POST /messages/send`
  - same idempotency key must not create duplicate jobs
- `POST /runtimes/register`
  - same runtime may re-register safely
- `POST /runs/{run_id}/complete`
  - exact replay for same valid lease may be treated idempotently
- `POST /runs/{run_id}/fail`
  - exact replay for same valid lease may be treated idempotently

## Pagination Rules
- Default list order is descending creation time unless otherwise documented.
- Event order is ascending event sequence.
- Pagination uses opaque cursors.

## Authentication
V1 authentication is bearer-token based.

- Runtime endpoints accept only the configured runtime token.
- Orchestration/operator endpoints accept only the configured operator token.
- A token valid for one surface must not authorize the other surface.
- `/health` remains unauthenticated for basic liveness checks.
