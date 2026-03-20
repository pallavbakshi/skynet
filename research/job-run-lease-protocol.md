# AGP Job / Run / Lease Protocol

## Status
Authoritative

## Purpose
This document defines the execution protocol between the control plane and runtimes for jobs, runs, and leases.

It is the source of truth for:
- work claiming
- heartbeats
- lease renewal
- fencing
- retry and reassignment
- duplicate terminal report handling

If another document conflicts with this one, this document wins.

## Core Entities

### Job
A durable unit of work tracked by the control plane.

### Run
A single execution attempt of a job.

### Lease
Temporary ownership of a run by an agent daemon executing on a runtime.

### Fencing Token
A monotonically increasing ownership token issued with a lease. Only the currently valid token may publish authoritative terminal state for a run.

## Authority Model
- The control plane is authoritative for all job, run, and lease state.
- The runtime is authoritative only for local execution observations before reporting them.
- Queue delivery is advisory transport and never authoritative.
- Artifact persistence must complete before terminal success or failure is accepted.

## Queue Model
- Work is distributed through:
  - per-agent FIFO queues
  - capability-pool queues
- A job targets exactly one queue at a time.
- Jobs addressed to a specific durable agent go to that agent's FIFO queue.
- Jobs addressed to a capability pool go to the queue owned by that capability.
- Queue redelivery is expected and must be tolerated.

## Claim Protocol

### Preconditions
A runtime may claim work only if:
- it is registered
- its `health_status` is not `unreachable`
- its `status` is not `draining`
- it either hosts the target agent or is eligible to instantiate the target capability

### Claim Steps
1. Runtime sends a claim request with:
- `runtime_id`
- claim scope:
  - hosted `agent_id`s and/or
  - eligible `capability_id`s
- current runtime status metadata

2. Control plane selects at most one eligible queued job.

3. Control plane creates:
- a new `run` if this is a new execution attempt
- a new `lease`
- a new `fencing_token`

4. Control plane marks:
- job `queued -> running`
- run `created -> leased`

5. Control plane returns:
- `job`
- `run`
- `lease`
- `fencing_token`
- `agent_id`
- artifact upload policy

### Empty Claim
- If no work is available, the control plane returns `204 No Content` or equivalent empty result.
- Runtime must back off before the next claim attempt.

## Lease Protocol

### Lease Acquisition
- A lease begins when the control plane grants ownership of a run.
- Lease ownership is scoped to:
  - `lease_id`
  - `run_id`
  - `agent_id`
  - `runtime_id`
  - `fencing_token`

### Heartbeats
- The runtime must heartbeat at a configured fixed interval.
- A heartbeat extends the lease expiry time.
- Heartbeats are independent from progress and terminal reporting.

### Heartbeat Failure Rule
- Missing three consecutive heartbeats causes lease expiry.
- Lease expiry invalidates the current fencing token.

### Lease Renewal
- Renewal is implicit through successful heartbeat.
- Renewal does not change run identity.
- Renewal may extend only an unexpired lease.

## Fencing Protocol

### Purpose
Fencing prevents expired or partitioned owners from publishing authoritative terminal state after the control plane has decided they no longer own the run.

### Rules
- Every lease has a unique fencing token.
- Terminal reports must include the fencing token.
- The control plane accepts terminal reports only if:
  - the lease is still valid
  - the fencing token matches the current live token for that run

### Lease Expiry
When a lease expires:
1. Control plane marks run `leased|running|recovering -> abandoned`
2. Control plane invalidates the fencing token
3. Control plane triggers fencing action through the runtime/SRE path
4. Control plane decides:
  - retry by requeueing the job if retry budget remains
  - fail the job if retry budget is exhausted

### Fencing Action
Fencing action must attempt to kill the old local execution context before reassignment.

The logical safety rule is:
- even if the old process survives briefly, it cannot successfully publish terminal state because its fencing token is invalid

## Progress Protocol
- Progress reports are optional but recommended.
- Progress reports must include:
  - `run_id`
  - `lease_id`
  - `fencing_token`
  - progress payload
- Progress reports from expired leases must be ignored or recorded as stale, but must not mutate authoritative state.

## Terminal Report Protocol

### Success
Success report requirements:
- valid `run_id`
- valid `lease_id`
- valid `fencing_token`
- artifact references already durably written

Control plane behavior:
1. validate lease and fencing token
2. validate role-aware artifact references
3. persist result artifact references
4. mark run `running -> completed`
5. mark job `running -> completed`
6. emit terminal events
7. release lease

### Failure
Failure report requirements:
- valid `run_id`
- valid `lease_id`
- valid `fencing_token`
- failure artifact reference if available

Control plane behavior:
1. validate lease and fencing token
2. validate role-aware artifact references
3. persist failure artifact references
4. mark run `running -> failed`
5. mark job `running -> failed`
5. emit terminal events
6. release lease

### Cancellation
Cancellation may occur through:
- operator interrupt
- orchestrator interrupt

For queued jobs:
1. control plane marks job `queued -> cancelled`
2. emit terminal cancellation events
3. no run or lease is created

For running jobs:
Control plane behavior:
1. mark job `running -> interrupt_requested`
2. propagate interruption intent to the owning runtime
3. runtime halts local execution
4. control plane marks:
  - run `running -> cancelled`
  - job `interrupt_requested -> cancelled`
5. release lease

Cancellation is terminal and is never automatically retried.

## Retry and Reassignment Policy

### Retryable Conditions
- lease expiry due to heartbeat loss
- unplanned runtime crash
- recoverable infrastructure failure before terminal state

### Non-Retryable Conditions
- explicit failure report from the runtime
- operator-initiated cancellation
- explicit agent teardown while busy

### Retry Behavior
- Retry always creates a new run.
- Previous run remains immutable as historical fact.
- Retry increments `retry_count`.
- Default retry budget is 3 attempts per job unless overridden by policy.

### Reassignment
- Reassignment is allowed only after the old lease is expired or explicitly released.
- Reassignment always uses a new lease and new fencing token.
- Reassignment may target:
  - the same durable agent after reprovision
  - another eligible runtime for the same capability pool

## Duplicate Terminal Reports

### Allowed
Duplicate delivery of the same terminal report for the same valid lease and fencing token may be treated as idempotent replay.

### Rejected
Terminal reports must be rejected if:
- fencing token is stale
- lease is expired
- run is already terminal under a different valid terminal transition

## Runtime Eligibility Semantics
- Runtime `status` describes operational process state.
- Runtime `health_status` is the authoritative routing-health classification.
- Claim and routing must exclude runtimes whose `health_status` is `unreachable`.
- Runtimes in `draining` status must not claim new work.

## Ordering Guarantees
- The control plane emits a monotonic event sequence.
- Event ordering is defined by control-plane-assigned sequence number, not client timestamps.
- Queue order is not authoritative global order.
- Per-agent FIFO ordering applies only to dequeue order for that specific queue, not to global event order.

## Required Events
At minimum:
- `job.accepted`
- `job.queued`
- `run.created`
- `lease.acquired`
- `run.running`
- `run.progress`
- `lease.heartbeat`
- `lease.expired`
- `run.completed`
- `run.failed`
- `run.cancelled`
- `job.completed`
- `job.failed`
- `job.cancelled`
- `job.requeued`

## Implementation Invariants
- A job has at most one active run.
- A run has at most one active lease.
- A lease has exactly one valid fencing token.
- Terminal state can be published only by the current valid lease owner.
- Retry never mutates the history of an earlier run.
- Queue redelivery never overrides state-store truth.
