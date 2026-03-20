# Technical PRD

## Document
AGP Phase 2 Technical PRD

## Version
0.1 Draft

## Purpose
Phase 2 expands AGP from a proven local control loop into a multi-runtime platform with richer orchestration semantics. The goal is to support multiple logical agents, distributed runtimes over a network, better recovery semantics, and a more complete operator/orchestrator surface.

Phase 2 is where AGP stops being a local prototype and becomes a genuine networked platform.

## Why Phase 2 Exists
Phase 1 proves the core loop. Phase 2 proves that the model survives distribution, heterogeneity, and higher orchestration complexity.

This phase exists to answer:
- can multiple runtimes coordinate safely through one control plane?
- can the orchestration layer stay simple while the platform scales out?
- can AGP support real handoffs and multi-agent workflows without losing state coherence?

## Phase Goal
By the end of Phase 2, AGP should support multiple distributed runtimes, multiple logical agents and capabilities, richer tracking and interruption semantics, and reliable cross-machine execution over a network.

## In Scope
- Distributed runtime registration and claiming
- Capability-based routing across multiple runtimes
- Better failure classification and retry policy
- Result routing and handoff support
- Improved watch and inspection APIs
- Artifact retrieval and pagination
- Runtime health classification
- Agent identity and capability registry
- Operator surfaces for active jobs, stale runtimes, and failure triage

## Out of Scope
- Full production Kubernetes deployment
- Multi-region architecture
- Multi-tenant billing and hard quotas
- Full session/message threading model
- Autonomous infrastructure repair

## Target Architecture

### Components
- `control-plane`
  - Shared network service
- `runtimes`
  - Multiple distributed execution-side services hosting durable agents
- `state-store`
  - Durable central source of truth
- `artifact-store`
  - Shared durable artifact backend
- `queue`
  - Shared work notification transport
- `orchestration-clients`
  - Human and agent-facing clients

### New Architectural Properties
- Capabilities are owned by the control plane as blueprints and may be instantiated into many durable agents.
- Multiple runtimes may host agents instantiated from overlapping capabilities.
- Jobs may be claimed only through platform-controlled lease semantics.
- Handoffs are orchestration-level actions backed by new jobs and artifacts.
- Runtime recovery remains local, but platform reassignment becomes normal.
- Per-agent FIFO ordering remains authoritative for jobs targeting a specific agent.

## Functional Requirements

### 1. Distributed Runtime Coordination
The platform must:
- support multiple runtimes connected over a network
- track runtime liveness independently from current work
- distinguish `idle`, `busy`, `degraded`, and `offline` runtime states
- support reassignment after lease expiry or runtime loss
- support durable agent identity independent of runtime churn

### 2. Capability Routing
The control plane must:
- instantiate agents from capability blueprints
- route work either to a specific agent queue or to a capability pool queue
- match capability-pool work to candidate runtimes by capability
- support priority order or deterministic tie-breaking
- avoid double assignment
- allow future extension for affinity and policy without changing the base model

### 3. Handoff and Result Routing
The orchestration layer must support:
- using one job result as input to another job
- explicit handoff metadata between jobs
- result references through artifact IDs rather than large inline payloads
- handoff fan-out as creation of distinct follow-on jobs, each with preserved provenance

### 4. Inspection and Operator Visibility
The platform must expose:
- active jobs
- recent failures
- stale runtimes
- current capabilities
- artifact references
- event timelines

### 5. Controlled Interruption
The platform must support:
- interruption requests on running jobs
- interruption visibility in the orchestration surface
- propagation of interruption intent to runtimes
- final resolved outcome after interruption

## API Requirements

### Additional Required Operations
- `GET /agents`
  - Returns durable logical agents, their capability blueprint, status, and queue state
- `GET /runtimes`
  - Returns runtime health and claimed work
- `GET /jobs`
  - Filterable list view for job inspection
- `GET /capabilities`
  - Returns routable capability registry
- `POST /jobs/{job_id}/handoff`
  - Creates follow-on work from an existing result
- `GET /artifacts/{artifact_id}/content`
  - Returns artifact data or paginated retrieval path
- `GET /health`
  - Returns platform health summary

### API Expectations
- All list and watch paths must be machine-readable and stable.
- Artifact APIs must support large output retrieval without requiring full inline payloads.
- The orchestration surface must still center on `send`, `watch`, `interrupt`, and `fetch`.
- Handoff must require an explicit source artifact set and produce explicit child `job_id` values.
- List and watch APIs must define ordering, pagination, and filtering contracts.

## Data Model Additions

### runtimes
- `runtime_id`
- `hostname` or logical location
- `status`
- `health_status`
- `last_heartbeat_at`
- `last_seen_at`
- `metadata`

### agent_runtime_bindings
- `agent_id`
- `runtime_id`
- `bound_at`
- `binding_status`

### handoffs
- `handoff_id`
- `source_job_id`
- `source_artifact_ids`
- `target_agent`
- `created_job_id`
- `created_at`

### health records
- `entity_type`
- `entity_id`
- `health_status`
- `reason`
- `observed_at`

### capability_pools
- `capability_id`
- `queue_id`
- `routing_policy`

## State Machine Extensions

### Runtime States
- `registering`
- `idle`
- `busy`
- `degraded`
- `offline`
- `draining`

### Job States
- `accepted`
- `queued`
- `running`
- `completed`
- `failed`
- `interrupt_requested`
- `cancelled`
- `blocked`

## Distributed Claim and Fencing Semantics
- Claim remains pull-based.
- A runtime may claim only work for agents it currently hosts or for capability-pool work it is eligible to instantiate.
- Each claim creates a lease with a fencing token.
- Heartbeat renewal extends the lease and proves continued ownership.
- Missing three consecutive heartbeats causes lease expiry.
- After lease expiry, the old owner must be fenced before reassignment is allowed to write terminal artifacts.
- Reassignment always creates a new run attempt with a new lease and fencing token.
- Duplicate terminal reports from expired leases must be rejected by fencing-token validation.

## Failure Semantics

### Runtime Failure Classification
The platform must distinguish at minimum:
- local runtime recoverable
- runtime unhealthy but reachable
- runtime unreachable
- run lease expired
- artifact upload failed
- control-plane-side interruption

### Platform Behavior
- Runtime-local recoverable failures remain local until recovery budget is exhausted.
- Lease expiry converts active ownership into a platform-visible problem.
- Reassignment always produces a new run attempt.
- Partial artifacts from failed runs must remain inspectable.
- Operator-initiated interruption remains terminal cancellation.
- Unplanned lease loss remains retryable up to policy budget.

## Reliability Requirements
- Heartbeats must be independent of result reporting.
- Runtimes must be able to reconnect after transient network loss.
- Control plane restarts must not lose job or run truth.
- Artifact references must remain stable even if runtimes disappear.
- Agent identity and queue state must survive runtime replacement.

## Security and Trust Assumptions
Phase 2 may still assume a trusted deployment boundary, but it must introduce:
- runtime identity
- explicit registration
- secret/config boundaries
- artifact access control hooks for future hardening

## Health Semantics
- `runtime.status` is the operational state of the runtime process.
- `health_status` is a derived health classification used for routing and operator visibility.
- At minimum the platform must distinguish:
  - `healthy`
  - `degraded`
  - `unreachable`
  - `draining`
- Routing must exclude `unreachable` runtimes and may exclude `degraded` runtimes by policy.

## Deliverables
- Networked multi-runtime support
- Capability registry and routing
- Expanded inspection APIs and CLI
- Handoff flow backed by artifacts and new jobs
- Better failure classification and retry control
- Shared artifact backend abstraction

## Acceptance Criteria
- Multiple runtimes on different machines can register and claim jobs safely.
- Jobs can be reassigned after runtime failure without corrupting state.
- One agent's result can be handed off as input to another agent through artifact references.
- Operators can inspect active jobs, failed runs, stale runtimes, and artifacts.
- The orchestration UX remains message-first despite the added backend complexity.
- Expired leases cannot successfully publish terminal results after reassignment.
- Durable agents remain addressable even when their hosting runtime is replaced.

## Test Strategy
- Multi-runtime integration tests
- Network partition and reconnect tests
- Duplicate claim prevention tests
- Lease expiry and reassignment tests
- Handoff and artifact-chaining tests
- Operator inspection API tests
- Fencing-token rejection tests
- Agent-runtime replacement tests

## Exit Criteria
Phase 2 is complete when AGP behaves as a coherent multi-runtime platform across machines, with stable orchestration semantics and durable system state.
