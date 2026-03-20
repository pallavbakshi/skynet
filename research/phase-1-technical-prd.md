# Technical PRD

## Document
AGP Phase 1 Technical PRD

## Version
0.1 Draft

## Purpose
Phase 1 establishes the minimum viable AGP control loop on a single machine or a very small trusted deployment. The goal is to prove that AGP can reliably accept work, assign it to a runtime, supervise execution, capture outputs, and expose a simple orchestration surface.

Phase 1 is not the full distributed platform. It is the foundation that validates the core product contract:

1. an orchestrator sends a message to an agent
2. the platform turns it into trackable work when needed
3. a runtime executes the work and reports back
4. the user receives a reply or a durable `job_id`

## Why Phase 1 Exists
Before building a distributed infrastructure-backed system, AGP needs a correct local execution model. If the control loop is ambiguous or unreliable on one machine, distributing it will only amplify failure modes.

Phase 1 exists to prove:
- the platform vocabulary is sound
- the orchestration surface is simple enough
- the runtime can supervise flaky agentic CLIs
- state, queueing, and artifacts have clear boundaries

## Phase Goal
By the end of Phase 1, AGP should support one logical orchestrator interacting with one or more logical agents through a control plane and runtime, with durable state and artifacts, on a single host.

## In Scope
- Control plane service
- Runtime supervisor service
- State store
- Artifact store
- Simple queue or claim mechanism
- Durable agent lifecycle and registration
- Capability blueprint registry owned by the control plane
- Message-to-job conversion for long-running work
- Run tracking, leases, heartbeats, and cancellation
- Basic orchestration API and CLI
- One concrete runtime adapter for an agentic CLI

## Out of Scope
- Multi-node production deployment
- Kubernetes-native hosting
- Multi-tenant auth and quotas
- Full session or conversation model
- Advanced scheduling policies
- Autonomous infra repair beyond local runtime recovery

## Target Architecture

### Top-Level Components
- `control-plane`
  - Central API and coordinator
- `runtime`
  - Execution-side service that supervises agentic CLIs and hosts agent daemons
- `state-store`
  - Canonical durable store for structured state
- `artifact-store`
  - Durable store for prompts, logs, results, and failure evidence
- `queue`
  - Work notification transport
- `orchestration-cli`
  - User-facing interface for sending, watching, interrupting, and fetching work

### Architectural Constraints
- The state store is the source of truth.
- The queue is transport, not truth.
- Runtime owns local recovery.
- Control plane owns global coordination state.
- The orchestration surface must not expose lease or queue mechanics directly.
- Agents are durable first-class entities tracked by the control plane.
- Capabilities are control-plane-owned blueprints from which agents are instantiated.
- Runtimes are ephemeral execution environments for durable agents.
- Jobs may target a specific agent queue or a capability pool queue.

## Technical Vocabulary
- `Message`
  - Orchestration-layer request addressed to a logical agent
- `Job`
  - Durable platform-tracked work unit created from a message when the work is not immediate
- `Run`
  - One execution attempt for a job
- `Lease`
  - Temporary ownership of a run by an agent daemon on behalf of a runtime-managed agent
- `Artifact`
  - Durable prompt, log, result, or failure evidence
- `Capability`
  - A control-plane-owned blueprint defining image, model, permissions, and resource policy

## Functional Requirements

### 1. Control Plane
The control plane must:
- accept `send` requests addressed to logical agents
- return either an immediate reply or a created `job_id`
- maintain the capability registry and agent registry
- persist jobs, runs, leases, and events
- expose APIs for `send`, `watch`, `get job`, `interrupt`, and `fetch artifact`
- coordinate runtime registration and liveness
- support cancellation requests
- maintain per-agent FIFO job queues
- apply the synchronous-vs-detached execution policy

### 2. Runtime Supervisor
The runtime must:
- register itself with the control plane
- claim work using a pull-based model
- start or reuse a local agentic CLI execution context
- monitor local execution continuously
- emit heartbeats and progress updates
- capture stdout/stderr and structured outputs as artifacts
- attempt bounded local recovery when execution fails
- escalate terminal failure back to the control plane
- fence dead local execution contexts before lease loss causes duplicate work

### 3. State Model
The system must persist at minimum:
- capabilities
- agents
- runtimes
- jobs
- runs
- leases
- artifacts
- events

### 4. Artifact Handling
Artifacts must support:
- prompt capture
- transcript logs
- exec logs
- final outputs
- failure records

All of the above must be durable.

The control plane must store only references and metadata for large outputs, not the outputs inline.

### 5. Orchestration Surface
The user-facing surface must provide:
- `send`
- `watch`
- `get result`
- `interrupt`
- `fetch artifact`

The orchestrator should reason in terms of agents, messages, jobs, and artifacts.

`send` must behave as follows:
- If the target agent is `IDLE` and completes within the configured detach threshold, return synchronously with the result reference.
- If the target agent is `BUSY`, return immediately with a `job_id`.
- If the target agent is `IDLE` but execution exceeds the configured detach threshold, detach and return `job_id`.

## API Requirements

### Required Operations
- `POST /messages/send`
  - Accepts a target logical agent and input payload
  - Returns either an immediate reply or `job_id`
- `GET /jobs/{job_id}`
  - Returns current job state and latest run summary
- `GET /jobs/{job_id}/events`
  - Returns ordered event history
- `POST /jobs/{job_id}/interrupt`
  - Requests interruption
- `GET /artifacts/{artifact_id}`
  - Returns artifact metadata and retrieval information
- `POST /runtimes/register`
  - Registers runtime identity and health metadata
- `POST /agents/up`
  - Instantiates a durable agent from a capability blueprint
- `POST /agents/{agent_id}/down`
  - Explicitly destroys a durable agent and its ephemeral runtime context
- `POST /runs/claim`
  - Pull-based claim path for runtimes
- `POST /runs/{run_id}/heartbeat`
  - Extends active lease
- `POST /runs/{run_id}/progress`
  - Publishes progress
- `POST /runs/{run_id}/complete`
  - Marks success and attaches result artifacts
- `POST /runs/{run_id}/fail`
  - Marks failure and attaches failure artifacts

### API Behavior
- All asynchronous work must have a `job_id`.
- All state transitions must be durable before the API reports success.
- The system must support idempotent retry for registration, claim response handling, and completion/failure reporting.
- Events must be durably ordered by a monotonic event sequence generated by the control plane.
- `send` response shape must be explicit and discriminated between `inline_result` and `accepted_async`.
- The detach threshold is a UX policy controlled by the control plane, not a storage or lease invariant.

## Data Model Requirements

### capabilities
- `capability_id`
- `image_ref`
- `model_ref`
- `resource_tier`
- `permission_profile`
- `queue_mode`
- `created_at`

### agents
- `agent_id`
- `capabilities`
- `capability_id`
- `assigned_runtime_id`
- `queue_id`
- `status`
- `last_seen_at`
- `workspace_ref`

### jobs
- `job_id`
- `target_agent_id`
- `target_queue`
- `message_ref`
- `status`
- `retry_count`
- `max_retries`
- `created_at`
- `updated_at`
- `latest_run_id`
- `result_artifact_id`

### runs
- `run_id`
- `job_id`
- `attempt`
- `agent_id`
- `runtime_id`
- `status`
- `started_at`
- `finished_at`
- `error_artifact_id`

### leases
- `lease_id`
- `run_id`
- `agent_id`
- `runtime_id`
- `expires_at`

### artifacts
- `artifact_id`
- `kind`
- `content_type`
- `storage_ref`
- `checksum`
- `size_bytes`
- `job_id`
- `run_id`
- `created_at`

### events
- `event_id`
- `job_id`
- `run_id`
- `event_type`
- `body`
- `created_at`

## State Machines

### Job States
- `accepted`
- `queued`
- `running`
- `completed`
- `failed`
- `interrupt_requested`
- `cancelled`

### Run States
- `created`
- `leased`
- `running`
- `recovering`
- `completed`
- `failed`
- `abandoned`
- `cancelled`

### Agent States
- `provisioning`
- `idle`
- `busy`
- `degraded`
- `draining`
- `terminated`

## State Transition Rules

### Job Transitions
- `accepted -> queued`
- `queued -> running`
- `running -> completed`
- `running -> failed`
- `queued -> cancel_requested -> cancelled`
- `running -> interrupt_requested -> cancelled`
- `running -> queued` only through lease expiry and retry policy
- `running -> failed` when retry budget is exhausted after lease-expiry recovery

### Run Transitions
- `created -> leased -> running`
- `running -> recovering` only within the same runtime-local recovery window
- `recovering -> running`
- `running -> completed`
- `running -> failed`
- `running -> cancelled`
- `leased|running -> abandoned` on lease expiry after heartbeat loss

### Lease Semantics
- A lease is acquired when an agent daemon claims work.
- The daemon must heartbeat every configured interval.
- Missing three consecutive heartbeats causes lease expiry.
- On lease expiry, the control plane and SRE path fence the local execution context and return the job to `queued` if retry budget remains.
- A forcibly interrupted job becomes `cancelled` and is not retried automatically.

## Failure Semantics

### Runtime-Local Failures
The runtime must attempt bounded recovery for:
- agent CLI crash
- transient prompt delivery failure
- local output capture failure
- stale local execution context

### Platform-Level Failures
The control plane must handle:
- lease expiry
- missing heartbeat
- duplicate completion/failure report
- runtime disconnect
- interruption while work is active
- forced agent teardown while work is active

### Retry Rules
- Local runtime recovery is bounded by policy.
- Platform retry creates a new run, not a mutated old run.
- Queue redelivery must never be treated as state truth.
- Lease-expiry retry budget defaults to three attempts per job.
- Operator-initiated teardown of a busy agent is authoritative cancellation, not automatic reassignment.
- Unplanned crash with expired lease is retryable up to budget.

## Consistency Model
- Artifacts are written first by the agent daemon to durable storage.
- Only after successful artifact persistence may the runtime report terminal success or failure to the control plane.
- The control plane updates job/run state only after receiving durable artifact references.
- If state update fails after artifact write succeeds, the artifact may be orphaned and must be garbage-collectable later.
- The system must never commit a successful terminal state pointing to a missing artifact.

## Implementation Decisions For Phase 1
- Claim model: pull-based
- Source of truth: relational state store
- Artifact storage: local filesystem or object-store-compatible abstraction
- Hosting model: local process or small trusted service deployment
- Orchestration UX: message-first
- Durable agent model: first-class agents instantiated from capabilities
- Queue policy: per-agent FIFO queue, with optional capability-pool queue support

## Deliverables
- Control plane service
- Runtime supervisor with one working agentic CLI adapter
- State schema and migrations
- Artifact storage abstraction
- Basic CLI or API client for orchestration
- End-to-end demo: send -> job -> run -> artifact -> result
- Lease heartbeat and fencing path
- Agent provisioning and teardown path

## Acceptance Criteria
- A user can send work to a logical agent and receive either an immediate reply or a `job_id`.
- A runtime can claim, execute, heartbeat, complete, or fail a run.
- The system survives agentic CLI instability through bounded local recovery.
- Prompts, transcript logs, exec logs, results, and failure evidence are persisted as artifacts and retrievable after completion.
- Job and run state remain coherent after runtime crash or restart.
- Lease expiry causes fencing and deterministic retry or failure according to policy.

## Test Strategy
- Unit tests for state transitions
- API tests for orchestration endpoints
- Integration tests for runtime claim/heartbeat/complete loop
- Failure injection for runtime crash and lease expiry
- Artifact persistence and retrieval tests
- Duplicate terminal-report tests
- Busy-agent interrupt and teardown tests

## Exit Criteria
Phase 1 is complete when AGP can reliably execute long-running agent work on a single host with durable state, durable artifacts, and a clean orchestration surface.
