# Dynamic Agent Mesh — PRD

## Status
Draft

## Summary
Evolve AGP from a pre-configured dispatch model to a dynamic agent mesh where agents self-register, discover each other, and communicate peer-to-peer through the control plane. Remove the bootstrap/seeding step entirely. Formalize the separation between AGP (agent coordination) and SkyOps (runtime infrastructure).

## Problem Statement

The current system requires a bootstrap service to pre-create capabilities, agents, and runtimes before any work can happen. This creates several issues:

1. **Rigid topology**: Every agent, capability, and runtime must be defined before the system starts. Adding a new agent means re-seeding or manual API calls.
2. **Conflated concerns**: The bootstrap mixes agent-layer concerns (creating logical agents) with infra-layer concerns (registering runtimes). The two audiences — developers using agents and operators managing infrastructure — share the same setup path.
3. **Separate runtime/agent lifecycle**: Runtimes and agents are tracked as separate database entities with a loose assignment link, adding complexity without clear benefit.
4. **No agent-to-agent discovery**: Agents cannot find each other dynamically. All routing requires knowing exact agent IDs, which must be pre-configured.

## Vision

A control plane where:
- Agents appear when their runtime starts and disappear when it stops
- Any agent can discover what other agents are online
- Any agent can send work to any other agent through the control plane
- No pre-configuration, no seeding, no bootstrap
- The control plane serves two distinct audiences through two logical API layers

## Two Layers, Two Audiences

### AGP Layer — Agent Coordination

**Audience**: Developers, orchestrator agents, external systems.

**Concern**: "I have agents. They talk to each other. I send work and get results."

The AGP layer exposes:
- **Registration**: Agent comes online, declares its identity and capabilities
- **Discovery**: "Who else is online? Who can do X?"
- **Messaging**: Agent-to-agent communication, always routed through the CP
- **Jobs**: Work tracking, status, artifacts, events
- **Heartbeat**: Liveness signal — the CP knows who is alive

The AGP layer does not know or care about containers, volumes, credentials, or process management.

### SkyOps Layer — Runtime Infrastructure

**Audience**: Operators, infra teams, monitoring systems.

**Concern**: "Are the containers healthy? Are credentials valid? What needs restarting?"

The SkyOps layer manages:
- **Runtime lifecycle**: Start, stop, restart containers that host agents
- **Credential management**: Shared volume, per-runtime env vars, token refresh
- **Health monitoring**: Container health, resource usage, alerting
- **Recovery**: If an agent process crashes inside a runtime, the runtime restarts it. From the AGP layer's perspective, the agent was online the whole time.
- **Observability**: Prometheus metrics, Grafana dashboards, audit logs

The SkyOps layer does not send messages to agents or route work. It manages the containers that host them.

### Relationship

```
Operator manages runtime (SkyOps)
  -> Runtime hosts agent process
    -> Agent registers with CP (AGP)
      -> Other agents discover and communicate with it (AGP)
```

The runtime is the **babysitter**. The agent is the **identity on the network**.

A runtime could restart its agent 10 times and from AGP's perspective, the agent was online the whole time — same ID, same heartbeat, no interruption. That is the runtime's job: keep the agent alive.

## Agent Lifecycle (New Model)

### Registration
1. Runtime container starts (managed by SkyOps or manually via `docker run`)
2. Runtime launches agent process
3. Agent calls `POST /agents/up` with:
   - `agent_id` (from env or config)
   - `capabilities` (self-declared list of what it can do)
   - `metadata` (optional: hostname, version, adapter type, etc.)
4. CP creates/updates the agent record, marks it `idle`, updates `last_heartbeat_at`
5. Agent begins heartbeat loop — periodic `POST /agents/up` calls (idempotent)

### Heartbeat

`/agents/up` serves as both registration and heartbeat. It is idempotent: the first call creates the agent record, subsequent calls update `last_heartbeat_at`. No separate heartbeat endpoint.

- Recommended interval: every 15 seconds
- Grace period: 60 seconds (configurable via `agent_heartbeat_grace_seconds`)
- If heartbeat stops, sweeper **deletes the agent record** after the grace period
- Deleted agents disappear from discovery results immediately
- If a deleted agent calls `/agents/up` again, it is re-created as a fresh registration

**Two heartbeat mechanisms coexist:**
1. **Lease heartbeat** (existing): Runtime extends lease TTL during active job execution via `/runs/{run_id}/heartbeat`. This is per-run, not per-agent.
2. **Agent heartbeat** (this): Runtime calls `/agents/up` periodically when idle (not executing a job). This is what drives discovery presence.

During active job execution, the lease heartbeat implicitly proves the agent is alive — no separate agent heartbeat needed. The agent sweeper will not delete an agent that has active leases (see Agent State Machine).

### Discovery
- Any agent can call `GET /agents` to see who else is available (all agents in the DB are alive)
- Filter by capability: `GET /agents?capability=code-review`
- Filter by status: `GET /agents?status=idle` (only agents ready for work)
- Response includes: agent ID, capabilities, metadata, status, last seen

### Communication
- Agent-to-agent messaging always flows through the CP
- `POST /messages/send` with `target.type=agent`, `target.id=<agent_id>`
- The CP queues the message for the target agent
- The target agent's runtime claims and executes it

### Routing Modes

Three routing patterns:

1. **Direct**: Sender knows the target agent ID, sends directly. `target.type=agent, target.id=coder-1`. Job goes to agent's queue.
2. **Discover-then-direct**: Sender queries `GET /agents?status=idle&capability=code`, picks a target, then sends directly. This is the primary new pattern enabled by agent discovery.
3. **Best-effort** (replaces capability-pool): Sender targets a capability, CP picks the best agent. `target.type=capability, target.id=code`. The CP resolves this by querying agents directly:
   ```sql
   SELECT * FROM agents
   WHERE 'code' = ANY(capabilities)
   AND status = 'idle'
   ORDER BY last_heartbeat_at ASC   -- least-recent = simple load balancing
   LIMIT 1;
   ```
   No `capability_pools` table needed. The routing policy (least-recent) is a CP-level default. If no matching idle agent exists, the job is queued and claimed when one becomes available.

The CP is always the intermediary. Agents never talk to each other directly.

### Shutdown
- Graceful: Agent calls `POST /agents/{id}/down` → CP **deletes the agent record** immediately
- Ungraceful: Heartbeat stops → sweeper **deletes the agent record** after grace period
- Operator-initiated: SkyOps kills the runtime → heartbeat stops → same as ungraceful

In all cases, the agent ceases to exist in the database. There is no "offline" state — an agent is either present (alive) or absent (gone). If it comes back, it calls `/agents/up` and is re-created.

### Agent State Machine

```
                  ┌──────────────────────────────────┐
                  │                                    │
  /agents/up      ▼         claim job                  │  /agents/up
 ───────────> [ idle ] ──────────────> [ busy ] ───────┘  (heartbeat)
                  │    <──────────────
                  │     job complete/fail
                  │
                  │  /agents/{id}/down          [ busy ]
                  │  (mode=drain)                  │
                  ▼                                │ /agents/{id}/down (mode=drain)
            [ draining ]                           ▼
                  │                          [ draining ]
                  │  queue empty                    │
                  ▼  (sweeper)                     │ queue empty (sweeper)
            [ DELETED ]  <─────────────────────────┘

  Also DELETED:
    - idle + heartbeat timeout (sweeper)
    - any + /agents/{id}/down (mode=force)
```

**States** (an agent in the DB is always in one of these):
- **idle**: Discoverable, ready to accept work.
- **busy**: Executing a job. Has an active lease. Not eligible for new claims.
- **draining**: Finishing current work, not accepting new jobs. Deleted when queue is empty.

There is no `offline` state. An agent that stops heartbeating is deleted after the grace period. An agent that is shut down is deleted immediately. If the same `agent_id` calls `/agents/up` later, it is re-created as a fresh record.

**Removed states** (from current model):
- `provisioning` → gone (agent is immediately `idle` after `/agents/up`)
- `degraded` → gone (binary: you exist or you don't)
- `terminated` → gone (replaced by deletion)
- `offline` → gone (replaced by deletion)

**Key rules:**
- Sweeper deletes: `idle` agents with stale heartbeat, `draining` agents with empty queue
- Sweeper does NOT delete `busy` agents — lease sweeper handles expired leases first, then the agent becomes `idle` (or is re-claimed), and is eligible for deletion on the next sweep if heartbeat is stale
- `/agents/{id}/down` with `mode=drain` transitions to `draining`; with `mode=force` deletes immediately
- Historical runs/leases retain the `agent_id` as a string even after the agent record is deleted (`runs.agent_id` becomes nullable)

## Data Model Changes

### Current
```
capabilities  ->  agents  ->  runtimes  (three separate tables, loose links)
                              agent_runtime_bindings (history)
                              capability_pools (routing config)
jobs -> runs -> leases  (all reference agent_id AND runtime_id as mandatory FKs)
```

Today, `agents.capability_id` is a NOT NULL foreign key — an agent cannot be registered without a pre-existing capability record. `runs.runtime_id` and `leases.runtime_id` are also mandatory FKs, meaning the runtime table cannot simply be deleted.

### Proposed
```
agents (primary identity table — only live agents exist here)
  - agent_id          (primary key)
  - status            (idle, busy, draining)
  - capabilities      (JSONB array, self-declared)
  - metadata          (JSONB, runtime info: hostname, adapter, version)
  - queue_id          (per-agent FIFO queue, format: "agent:{id}")
  - last_heartbeat_at
  - registered_at
  - updated_at

runtimes (internal, not exposed in AGP API)
  - runtime_id        (primary key)
  - agent_id          (FK to agents — 1:1 binding, set at registration)
  - status, health_status, last_seen_at, last_heartbeat_at
  - metadata_json     (hostname, release_version, adapter type)

runs (agent_id becomes nullable — survives agent deletion)
leases (agent_id becomes nullable — survives agent deletion)
```

Key changes:

- **Agents are ephemeral records.** An agent exists in the DB only while it is alive. When it stops heartbeating or shuts down, its record is deleted. There is no `offline` or `terminated` state. All agents in the table are live.
- **Capabilities** move from a separate table to a self-declared JSONB array on the agent record. The `capability_id` FK on agents becomes nullable during the transition, then removed. An agent can declare any capability string — no pre-seeding required.
- **Capability-pool routing** no longer needs the `capability_pools` table. When a message targets `type=capability`, the CP queries agents directly by their JSONB `capabilities` field and picks the least-recently-active idle agent. The `capability_pools` table is removed.
- **Runtimes stay in the database** but become an internal implementation detail. The `runtimes` table is needed because `runs` and `leases` reference `runtime_id` as mandatory FKs. Runtimes are created automatically when an agent registers (one runtime per agent), and managed by SkyOps for health/restart.
- **`assigned_runtime_id` on agents is removed**. Instead, `runtimes.agent_id` points to the agent.
- **`agent_runtime_bindings` table** is removed. With 1:1 agent-runtime binding set at registration, the history table is unnecessary.
- **`runs.agent_id` and `leases.agent_id` become nullable**. When an agent is deleted, historical runs and leases retain the agent_id value but the FK constraint no longer enforces existence. This preserves audit history.

## API Changes

### AGP Routes (agent-facing)

**Core (new or changed):**

| Method | Path | Description | Status |
|--------|------|-------------|--------|
| POST | `/agents/up` | Register/heartbeat (idempotent) | Changed: accepts `capabilities` array, doubles as heartbeat |
| POST | `/agents/{id}/down` | Shutdown (delete) or drain | Changed: `mode=force` deletes record |
| GET | `/agents` | List/discover agents | Changed: filter by `capability` string; all results are live |
| GET | `/agents/{id}` | Get agent details | Unchanged |
| POST | `/messages/send` | Send message to agent or capability | Unchanged |
| GET | `/jobs`, `/jobs/{id}` | Job listing and details | Unchanged |
| GET | `/jobs/{id}/events` | Stream job events (SSE) | Unchanged |
| GET | `/health` | CP health check | Unchanged |

**Job execution (runtime-internal, unchanged):**

These endpoints are used by the runtime supervisor during job execution. They are not part of the agent-facing API but are fundamental to how work happens. All unchanged.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/runtimes/register` | Runtime self-registration |
| POST | `/runs/claim` | Claim a queued job |
| POST | `/runs/{id}/heartbeat` | Extend lease during execution |
| POST | `/runs/{id}/progress` | Report execution progress |
| POST | `/runs/{id}/complete` | Mark execution complete |
| POST | `/runs/{id}/fail` | Mark execution failed |
| POST | `/runs/{id}/cancel` | Cancel execution |
| POST | `/runs/{id}/recovering` | Signal crash recovery |
| POST | `/runs/{id}/resumed` | Signal recovery complete |

**Artifacts (unchanged):**

| Method | Path | Description |
|--------|------|-------------|
| POST | `/artifacts/upload` | Upload artifact |
| GET | `/artifacts/{id}` | Artifact metadata |
| GET | `/artifacts/{id}/content` | Stream artifact content |

**Existing endpoints kept as-is:**

| Method | Path | Note |
|--------|------|------|
| PATCH | `/agents/{id}` | Update agent fields |
| POST | `/agents/{id}/interrupt` | Interrupt agent work |
| POST | `/agents/{id}/undrain` | Resume from draining |
| POST | `/jobs/{id}/interrupt` | Cancel a job |
| POST | `/jobs/{id}/block` | Block a job |
| POST | `/jobs/{id}/unblock` | Unblock a job |
| POST | `/jobs/{id}/handoff` | Handoff to other agents |
| GET | `/capabilities` | List capability registry (optional, read-only) |
| POST | `/capabilities/seed` | Create/update capability (deprecated after Phase 3) |
| POST | `/nudges` | Create nudge |
| GET | `/nudges/next` | Claim next nudge |

### SkyOps Routes (ops-facing)

These are new endpoints, namespaced under `/ops/`. Existing observability and system endpoints migrate here over time.

| Method | Path | Description | Status |
|--------|------|-------------|--------|
| GET | `/ops/runtimes` | List managed runtimes | New (replaces `/runtimes`) |
| POST | `/ops/runtimes/{id}/restart` | Restart a runtime | **New** |
| POST | `/ops/runtimes/{id}/drain` | Drain and stop a runtime | **New** |
| GET | `/ops/health` | Infrastructure health | New (wraps `/observability/summary`) |
| GET | `/ops/alerts` | Active alerts | New (wraps `/observability/alerts`) |
| GET | `/ops/audit` | Audit log | New (wraps `/observability/audit`) |
| GET | `/ops/metrics` | Prometheus metrics | New (wraps `/observability/metrics`) |

Existing `/observability/*` and `/system/*` endpoints remain available during the transition but are considered deprecated once `/ops/*` equivalents exist.

## What Gets Removed

1. **Bootstrap service**: Entirely removed from compose and codebase. No more pre-seeding.
2. **`capabilities` table as a hard requirement**: `agents.capability_id` FK becomes nullable, then removed. Capabilities become self-declared via JSONB array.
3. **`capability_pools` table**: Removed. Best-effort routing queries the agents table directly instead of maintaining a separate pool table with routing config.
4. **`assigned_runtime_id` on agents**: Replaced by `runtimes.agent_id` (runtime points to agent, not the other way around). No manual assignment step.
5. **`agent_runtime_bindings` table**: Removed. 1:1 agent-runtime binding eliminates need for history tracking.
6. **Bootstrap script** (`scripts/bootstrap_local_stack.py`): Deleted.
7. **`/runtimes` public endpoints**: Runtime listing/details move to `/ops/runtimes`. Runtimes are no longer part of the agent-facing API.
8. **`offline`/`terminated` agent states**: Dead agents are deleted, not marked. The DB only contains live agents.

**Note**: The `runtimes` table itself is NOT removed. It stays as an internal implementation detail because `runs` and `leases` reference `runtime_id` as mandatory foreign keys. Runtimes are created automatically during agent registration and managed by SkyOps — they just stop being a first-class concept in the AGP API.

## What Stays

1. **Control plane**: Same process, same DB, same port. Just different routes and registration flow.
2. **Sweepers**: Lease sweeper (reclaim expired job leases) and agent sweeper (delete dead agents). The runtime sweeper merges into the agent sweeper in Phase 3.
3. **Job/run/lease model**: How work is tracked and executed doesn't change. A message creates a job, a runtime claims it, executes it, completes it. The `/runs/*` endpoints (claim, heartbeat, progress, complete, fail, cancel, recovering, resumed) are all unchanged.
4. **Artifact storage**: S3/MinIO for large outputs. All `/artifacts/*` endpoints unchanged.
5. **Queue transport**: Redis for work notifications. Per-agent queues (`agent:{id}`) stay.
6. **Best-effort routing**: `POST /messages/send` with `target.type=capability` continues to work. The CP resolves it by querying agents' self-declared capabilities directly — no `capability_pools` table needed.
7. **Nudges**: `/nudges/*` endpoints for agent orchestration signals. Unchanged.
8. **SkyOps CLI**: `skyops up` (control plane infra), `skyops runtime auth`, `skyops runtime env`, `skyops runtime attach`. All still needed.
9. **Credentials volume**: Shared `agp-credentials` volume for tool credentials and env vars. Unchanged.
10. **`runtimes` table**: Stays as internal implementation detail. Required by runs/leases FK constraints.

## Implementation Phases

### Phase 1: Self-Registration (removes bootstrap)
- Make `/agents/up` idempotent: first call creates, subsequent calls update `last_heartbeat_at` (heartbeat)
- Extend auth middleware to require runtime bearer token on `/agents/up` and `/agents/{id}/down`
- RuntimeSupervisor calls `/agents/up` on startup (currently only calls `/runtimes/register`)
- RuntimeSupervisor calls `/agents/up` periodically as heartbeat (every 15s)
- RuntimeSupervisor calls `/agents/{id}/down` on graceful shutdown
- Agent declares its ID and capabilities from env vars
- Make `agents.capability_id` nullable (migration) — agents can register without a pre-existing capability
- Add `capabilities` JSONB field to agents table (migration)
- Make `runs.agent_id` and `leases.agent_id` nullable (migration) — survive agent deletion
- Sweeper **deletes** agents when heartbeat stops (not mark offline)
- Sweeper uses conditional delete to avoid heartbeat race
- Add lease refresh on CP startup
- Remove bootstrap service from compose
- Remove bootstrap script

### Phase 2: Agent Discovery
- Add filtering to `GET /agents`: `?status=idle&capability=X` (query JSONB array)
- Agents can query the CP to find available peers
- Add composite index on `(status, capabilities)` for discovery performance
- This enables orchestrator agents to dynamically find and use worker agents

### Phase 3: Data Model Simplification
- Remove `agents.capability_id` column (all agents use JSONB `capabilities`)
- Remove `capabilities` table (or keep as optional read-only registry)
- Remove `capability_pools` table (best-effort routing queries agents directly)
- Add `runtimes.agent_id` FK, remove `agents.assigned_runtime_id`
- Remove `agent_runtime_bindings` table
- Simplify sweepers: merge runtime sweeper into agent sweeper (one sweeper for agent liveness, one for lease expiry)
- Remove `provisioning`, `degraded`, `terminated`, `offline` from agent status enum (only `idle`, `busy`, `draining` remain)

### Phase 4: API Namespacing
- Add `/ops/*` routes wrapping existing observability/system endpoints
- Deprecate `/runtimes`, `/observability/*`, `/system/*` routes
- Clear separation in code: `agp/api/routes/agents.py` vs `agp/api/routes/ops.py`

## Example Flow

### Orchestrator finds and uses a worker

```
1. Runtime-A starts -> Agent "orc-1" registers with CP
     POST /agents/up {agent_id: "orc-1", capabilities: ["orchestrate"]}

2. Runtime-B starts -> Agent "coder-1" registers with CP
     POST /agents/up {agent_id: "coder-1", capabilities: ["code", "python"]}

3. Orc-1 discovers available agents:
     GET /agents?status=idle&capability=code
     -> [{agent_id: "coder-1", capabilities: ["code", "python"], status: "idle"}]

4. Orc-1 sends work to coder-1:
     POST /messages/send {target: {type: "agent", id: "coder-1"}, message: {text: "fix the bug in auth.py"}}

5. Coder-1's runtime claims the job, executes it, completes it.

6. Orc-1 checks the result:
     GET /jobs/{job_id}
     -> {status: "completed", artifacts: [...]}
```

### Agent disappears

```
1. Runtime-B crashes (coder-1 goes silent)
2. Heartbeat (/agents/up) stops arriving
3. Sweeper runs -> deletes coder-1 from agents table
4. Orc-1 queries discovery -> coder-1 is gone
5. Orc-1 finds another agent or waits and retries discovery
```

### Agent comes back

```
1. Runtime-B restarts -> calls POST /agents/up {agent_id: "coder-1", capabilities: ["code", "python"]}
2. CP creates a fresh agent record for coder-1
3. Orc-1 queries discovery -> coder-1 is back
```

### Operator kills an agent

```
1. Operator runs: docker stop runtime-b
2. Runtime-B shuts down -> agent calls /agents/down (if graceful) -> record deleted
3. Or heartbeat stops -> sweeper deletes after grace period
4. From AGP's perspective: coder-1 simply doesn't exist anymore
```

## Security

### Registration Authentication

All agent and runtime registration endpoints require a bearer token (`AGP_RUNTIME_BEARER_TOKEN`). This is the same token currently used for `/runs/*` endpoints, extended to cover `/agents/up` and `/agents/{id}/down`.

Without this, any process that can reach the CP can register as any agent — hijacking work queues, impersonating agents, or exhausting resources with fake registrations.

**Enforcement**: The auth middleware must treat `/agents/up` and `/agents/{id}/down` as runtime-write operations requiring the runtime bearer token. This is a configuration fix, not a protocol change.

**Production requirement**: `AGP_RUNTIME_BEARER_TOKEN` MUST be set in any non-local deployment. The CP should log a warning at startup if it's unset.

### Lease Fencing

The existing fencing token mechanism (lease attempt counter) prevents stale runtimes from completing jobs they no longer own. This stays unchanged. However, the heartbeat and complete endpoints should also validate that the requesting runtime's `agent_id` matches the lease's `agent_id` — preventing cross-agent interference.

## Operational Concerns

### Control Plane Restarts

**Problem**: When the CP restarts, all active leases have `expires_at` timestamps that may now be in the past. The lease sweeper runs immediately and marks everything as expired/abandoned — losing in-flight work even though runtimes are still executing.

**Mitigation**: On CP startup, before the sweeper starts, refresh all active leases:
```
UPDATE leases SET expires_at = now() + interval '{default_ttl} seconds'
WHERE status = 'active';
```

This gives runtimes time to re-establish their heartbeat loop. The first heartbeat after CP restart confirms the runtime is alive; a missed heartbeat after the refreshed TTL means the runtime genuinely died during the outage.

### Network Partitions

If an agent can't reach the CP:
1. Agent heartbeats (`/agents/up`) stop arriving → sweeper deletes agent after grace period
2. In-flight lease heartbeats also timeout → lease expires → job requeued
3. When connectivity restores, agent calls `/agents/up` again → re-created as `idle`

**Self-recovery is automatic**: A deleted agent that calls `/agents/up` is re-created. No operator intervention needed for transient network issues. The agent gets a fresh record but the same `agent_id`, so it rejoins the mesh seamlessly.

### Sweeper Race Conditions

The agent sweeper evaluates `last_heartbeat_at < cutoff` to delete stale agents. A heartbeat arriving between the read and the delete could be lost.

**Mitigation**: The sweeper uses a conditional delete:
```sql
DELETE FROM agents
WHERE agent_id = :id
AND last_heartbeat_at < :cutoff
AND status != 'busy';
```

If a heartbeat arrived and updated `last_heartbeat_at` after the sweeper read it, the WHERE clause no longer matches and the delete is a no-op. Busy agents are never deleted by the sweeper — lease expiry handles them first.

## Migration Strategy

### For Existing Deployments (Bootstrap-Era Data)

Existing deployments have capabilities, agents, and runtimes created by the bootstrap script. The migration path:

1. **Phase 1 (backward-compatible)**: Bootstrap still works but is no longer required. The compose file removes the bootstrap service. Operators who need pre-created capabilities can use `POST /capabilities/seed` directly.

2. **Capability FK relaxation**: A migration makes `agents.capability_id` nullable. Existing agents keep their FK; new agents registered via `/agents/up` with self-declared capabilities have `capability_id = NULL` and use the new `capabilities` JSONB field instead.

3. **Runtime auto-creation**: When an agent calls `/agents/up`, the CP checks if a runtime exists for this agent. If not, it creates one internally. Existing runtimes that were bootstrap-created continue to work — they just get linked to their agent via the new `runtimes.agent_id` column.

4. **No data loss**: No tables are dropped in Phase 1-2. The `capabilities` table and `runtimes` table remain. They become less central to the flow but are not deleted.

5. **Cleanup (Phase 3+)**: After all agents have re-registered with the new model, the `capabilities` table, `capability_pools` table, and `agent_runtime_bindings` table can be dropped. A migration script will identify orphaned records.

### For Tests

Tests currently seed `cap_python` in `setUp()`. This continues to work unchanged through Phase 1-2. In Phase 3, tests switch to the self-registration model (agents declare capabilities in `/agents/up`).

## Non-Goals

- **Agent-to-agent direct networking**: All communication goes through the CP. Agents never connect to each other directly.
- **Automatic scaling**: SkyOps does not auto-scale runtimes based on demand (yet). An operator starts/stops runtimes manually or via external orchestration.
- **Multi-tenancy**: No tenant isolation in this phase. All agents share one CP.
- **Capability negotiation**: An agent declares capabilities at registration. There is no protocol for agents to negotiate or change capabilities at runtime.

## Success Criteria

1. A fresh `skyops up` starts the control plane with zero agents in the database. No bootstrap needed.
2. A `docker run` of a runtime container results in an agent appearing in `GET /agents` within seconds.
3. Stopping that container results in the agent being deleted from the database within the heartbeat grace period (60s default).
4. Restarting that container results in the agent re-appearing in `GET /agents` — same ID, fresh record.
5. An orchestrator agent can discover and send work to a worker agent without any pre-configuration.
6. `POST /messages/send {target.type: "capability", target.id: "code"}` routes to an idle agent with that capability — no pre-seeded capability table needed.
7. An operator can see runtime health via SkyOps without touching the AGP agent layer.
