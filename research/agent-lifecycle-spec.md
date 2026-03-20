# AGP Agent Lifecycle Specification

## Status
Authoritative

## Purpose
This document defines the lifecycle of durable agents.

It covers:
- provisioning
- queue ownership
- execution behavior
- idle timeout
- draining
- teardown
- reprovisioning

## Agent Model
- An agent is a durable first-class entity tracked by the control plane.
- An agent is instantiated from a capability blueprint.
- An agent may outlive the specific runtime process currently hosting it.
- An agent owns:
  - identity
  - queue
  - workspace reference
  - lifecycle state

## Creation: `agp up`

### Inputs
- `capability_id`
- optional human-readable agent name

### Behavior
1. Control plane allocates `agent_id`
2. Control plane creates per-agent FIFO queue
3. Control plane creates agent record in `provisioning`
4. Control plane assigns or provisions a runtime host
5. Runtime prepares local execution context
6. Agent becomes `idle`

## Execution Behavior
- An idle agent may accept queued work.
- A busy agent owns exactly one active run at a time.
- Additional jobs for that agent accumulate in its FIFO queue.
- FIFO ordering is authoritative for jobs targeting that agent.

## Capability Pools
- Some work may target a capability pool instead of a named agent.
- Capability-pool work may instantiate or bind an eligible agent according to routing policy.
- Once bound for execution, the resulting run still executes under a concrete agent identity.

## Idle Timeout
- An idle timeout policy may terminate long-idle agents.
- Idle timeout applies only to agents with:
  - no active run
  - no draining activity
- Before timeout termination:
  - queue must be empty
  - no active lease may exist

## Draining
- Draining prevents acceptance of new jobs.
- An agent may enter `draining` because of:
  - operator request
  - planned runtime maintenance
  - reprovisioning

### Draining Rules
- queued work remains queued unless explicitly migrated by policy
- active work may finish or be cancelled
- once no active work remains, agent may:
  - return to `idle` if draining is lifted
  - transition to `terminated` if teardown is requested

## Destruction: `agp down`

### Graceful Down
1. agent enters `draining`
2. no new jobs accepted
3. active run allowed to finish or be explicitly cancelled
4. queue must be empty or manually handled
5. local workspace cleaned
6. agent becomes `terminated`

### Force Down
1. active run is cancelled
2. queued jobs for that agent are cancelled unless explicitly re-dispatched by the orchestrator
3. local execution context is torn down
4. agent becomes `terminated`

Force-down is authoritative cancellation, not automatic reassignment.

## Reprovisioning
- An agent may be reprovisioned after:
  - runtime crash
  - lease expiry
  - planned maintenance

### Reprovision Rules
- Durable agent identity remains the same.
- Ephemeral runtime context may change.
- Workspace may be reconstructed according to runtime policy.
- Active job retry, if allowed, creates a new run.

## Workspace Rules
- Agent workspace is ephemeral and runtime-local unless otherwise configured.
- Clean-slate cleanup must occur after:
  - successful completion if required by policy
  - failure if required by policy
  - cancellation
  - force teardown

## Queue Ownership Rules
- Each durable agent owns exactly one FIFO queue.
- Jobs addressed to that agent enter that queue.
- Queue ownership ends only when the agent is terminated.

## Lifecycle Invariants
- An agent ID is never reused.
- A terminated agent cannot accept new work.
- A busy agent cannot own more than one active run.
- Idle timeout cannot terminate an agent with queued or running work.
- Force-down of a busy agent cancels current work rather than silently reassigning it.
