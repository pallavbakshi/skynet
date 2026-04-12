# AGP Patterns Guide

A practical guide to AGP's architecture, data flow, and internal patterns for developers working on or with the codebase.

## Architecture at a Glance

```
Operator (CLI / SDK)
    │
    ▼
Control Plane (FastAPI, SQLite/Postgres)
    │                    ▲
    │  heartbeat +       │  complete/fail +
    │  directives        │  peek results
    ▼                    │
Runtime Supervisor ──────┘
    │
    ▼
Agent Process (Claude Code, Codex, etc.)
    │
    ▼
Terminal Host (tmux / wezterm session)
```

**Control Plane (CP)** — single FastAPI process that owns all state. Exposes REST API. Stores jobs, runs, leases, agents, runtimes, events in SQLite (dev) or Postgres (prod). In-memory singletons for ephemeral state (PeekStore, NudgeStore).

**Runtime** — a supervisor process that runs on the same machine as the agent. Heartbeats to the CP every ~15s via `POST /agents/up`. Claims work, executes it in a terminal session, reports results back. Multiple runtimes can connect to one CP from different machines.

**Agent** — a logical identity registered with the CP. Has capabilities (e.g. `code`, `review`), a status (`idle`, `busy`, `draining`), and is bound to exactly one runtime.

## The Job Lifecycle

```
Message → Job → Run → Lease → Execution → Complete/Fail
```

1. **Message** — operator sends text + optional attachments via `POST /messages/send`
2. **Job** — created from the message, placed in a target queue (`agent:claude-dev` or `capability:review`)
3. **Claim** — runtime calls `POST /runs/claim`, dequeues a job, creates a Run and Lease
4. **Execution** — runtime's adapter (claude_code, codex) drives the agent in a terminal session
5. **Completion** — runtime calls `POST /runs/{id}/complete` with artifacts and summary
6. **Failure** — if execution fails, runtime calls `POST /runs/{id}/fail`

### Fencing Tokens

Every lease has a `fencing_token` (monotonically increasing per job). The complete/fail endpoints reject stale tokens. This prevents a slow runtime from overwriting results after its lease expired and another runtime re-ran the job.

### Retry Logic

Jobs have `max_retries` (default 3). When a lease expires (runtime dies or is too slow), the sweeper increments `retry_count` and requeues the job — unless retries are exhausted or `deadline_at` has passed. **Important**: explicit `POST /runs/{id}/fail` does NOT trigger retries. It's treated as a terminal failure. Only lease expiry triggers the retry path.

## Queue Routing

Jobs are routed by queue name, not by direct assignment:

- `agent:claude-dev` — direct-to-agent queue (only that agent claims it)
- `capability:review` — capability queue (any agent with `review` capability claims it)

A runtime's `POST /runs/claim` checks queues in priority order: its agent-specific queue first, then capability queues. This means direct-targeted jobs always take priority.

### Queue Backends

| Backend | Use Case | Concurrency |
|---|---|---|
| `db` (DbQueueBackend) | Production | SQL-level locking, safe for multiple workers |
| `inmemory_broker` | Dev/test | No thread safety — single-process only |

The backend is selected by `settings.queue_backend`. Do not use `inmemory_broker` with multiple workers.

## The Heartbeat / Directive System

Runtimes heartbeat to the CP every ~15s via `POST /agents/up`. The response can include **directives** — instructions piggybacked on the heartbeat:

```python
# CP response to agent_up:
{
    "data": { ...agent fields... },
    "_directives": {
        "peek_requested": true,
        "peek_request_id": "peek_abc123",
        "peek_lines": 0
    }
}
```

Current directives:
- **peek_requested** — capture terminal content and POST it back to CP
- **nudge_requested** — inject text into the agent's terminal

This is a pull-based model: the CP stores pending requests (in PeekStore/NudgeStore), and the runtime picks them up on the next heartbeat. Latency is 0-15s depending on timing.

**Implication for remote access**: Peek/nudge work across machines as long as CLI and runtime talk to the same CP. The PeekStore is in-memory, so multi-process CP deployments (e.g. gunicorn workers > 1) will break — the POST and heartbeat may hit different processes.

## The Sweep System

A background loop (`sweep_loop`) runs every 10-30s and handles state cleanup:

| Phase | What it does |
|---|---|
| `sweep_expired_leases` | Expire leases past TTL, abandon runs, requeue or fail jobs |
| Phase 1 | Delete idle agents with stale heartbeats |
| Phase 2 | Delete draining agents with no remaining work |
| Phase 3 | Mark runtimes as degraded/offline based on heartbeat age |
| Phase 4 | Resume runtimes that start heartbeating again |
| Phase 5 | Fail orphaned queued jobs whose target agent no longer exists |

### FK Ordering Constraint

When deleting an agent, `_nullify_agent_references()` must run BEFORE the `DELETE FROM agents` statement because the SQLite migration DDL lacks `ON DELETE SET NULL`. The nullification uses a guard clause matching the same deletion predicate to avoid clearing references for agents that recovered between the SELECT and DELETE.

## The Adapter Plugin System

Adapters bridge the runtime supervisor to specific agent processes:

```
src/agp/plugins/
├── claude_code/
│   └── adapter.py      # Drives Claude Code CLI in tmux
├── codex/
│   └── adapter.py      # Drives Codex CLI in tmux
└── _via_file.py         # Shared: builds task files for agents
```

Each adapter implements:
- **Bootstrap** — start the agent process in a terminal session
- **Send task** — write the task to a file, invoke the agent
- **Collect result** — parse agent output, extract artifacts
- **Health check** — detect if the agent process is still alive

### The Via-File Pattern

Complex prompts are delivered via temp files rather than command-line arguments:

```python
prompt_text, sections_text = build_task_content(
    prompt=prompt, claimed=claimed, attachments=attachments
)
# prompt_text → inside BEGIN TASK / END TASK markers
# sections_text → metadata after END TASK (agent ID, job ID, etc.)

# If the CLI sent --via-file, pass the original path directly:
if via_file_path:
    response = so.send(file=via_file_path, sections=sections_text)
else:
    response = so.send(prompt_text, sections=sections_text)
```

This avoids shell quoting issues and double file wrapping.

## The Artifact System

Artifacts are stored outside the DB — only metadata (storage_ref, checksum, size) lives in the DB.

| Backend | Storage |
|---|---|
| `LocalFsArtifactStore` | `./agp-artifacts/` on disk |
| `SharedFsArtifactStore` | Shared filesystem mount |
| `RegistryFsArtifactStore` | Object + metadata sidecar files |
| `S3ArtifactStore` | S3-compatible object store |
| `HttpArtifactStore` | HTTP endpoint (runtime → CP upload) |

**Path traversal protection**: All backends pass artifact names through `_sanitize_name()` which rejects `..`, `/`, `\`, and empty strings.

Artifacts are linked to jobs via `JobArtifact` (role-based: `result`, `prompt`, `transcript_log`, `exec_log`) and to runs via `RunArtifact`.

## Transaction Boundaries

There is no single convention. Some service functions commit internally, others expect the route to commit:

- `execute_claim()` — commits internally (creates run, lease, transitions states atomically)
- `complete_run_service()` — commits internally
- Most route handlers in `jobs.py`, `admin.py` — commit in the route after calling services

**Rule of thumb**: don't compose multiple service calls expecting a single transaction unless you've verified neither commits internally.

## The Event System

Every state transition emits an event:

```python
_create_event(db, job_id=..., event_type="run.created", body={...})
```

Events have a monotonically increasing `event_seq` (SQLite sequence table or Postgres sequence). Consumers poll `GET /jobs/{id}/events?cursor=...` for streaming.

Event types: `job.created`, `job.completed`, `job.failed`, `run.created`, `run.failed`, `lease.acquired`, `lease.expired`, `agent.busy`, `agent.deleted`, `routing.decision`, etc.

## API Patterns

### Response Envelope

All API responses use a standard envelope:

```json
{"ok": true, "data": { ... }}
{"ok": false, "error": {"code": "not_found", "message": "agent not found: xyz"}}
```

### Cursor-Based Pagination

List endpoints use cursor-based pagination:

```json
{
    "items": [...],
    "page": {
        "limit": 50,
        "next_cursor": "eyJjcmVhdGVkX2F0Ijo..."
    }
}
```

Cursors are base64-encoded JSON with required fields (e.g. `created_at`, `job_id`). The `_cursor_field()` helper validates required fields and returns 400 on missing keys.

### Route Namespaces

| Namespace | Purpose | Auth |
|---|---|---|
| `/agents/*`, `/messages/*` | Operator-facing | Operator token |
| `/runs/*`, `/runtimes/*` | Runtime-facing | Runtime token |
| `/ops/*` | Operational dashboards | Operator token |
| `/health` | Health check | None |

## Common Pitfalls

### SQLite vs Postgres Divergence

- SQLite migration DDL lacks `ON DELETE SET NULL` — FK cascades don't work
- SQLite uses `BEGIN IMMEDIATE` for write serialization — no concurrent write races
- Postgres uses READ COMMITTED — concurrent heartbeats can race with sweeper operations
- `PRAGMA foreign_keys=ON` is set at connection time but only affects SQLite

### In-Memory Singletons

`PeekStore`, `NudgeStore`, and `InMemoryBrokerQueueBackend` are process-local singletons. They break with:
- Multi-process CP (gunicorn workers > 1)
- Multiple CP instances behind a load balancer

### The Death Loop

If the supervisor's `complete()` call is rejected by the CP (lease expired, fencing token stale), the result is lost. The supervisor logs this and moves on rather than retrying, because retrying with the same stale token would fail forever ("death loop"). The job eventually gets cleaned up by the lease expiry sweeper.

### Heartbeat Timing and Remote Peek

The runtime heartbeats every 15s. Peek requests are delivered via heartbeat directives. For remote agents (tunneled connections), the round-trip can take 10-20s. The default CLI peek timeout is 45s to accommodate 2 heartbeat cycles plus network overhead.
