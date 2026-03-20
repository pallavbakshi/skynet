# AGP State Machine Specification

## Status
Authoritative

## Purpose
This document defines the legal states and legal transitions for AGP entities.

It is the source of truth for:
- `Agent`
- `Runtime`
- `Job`
- `Run`
- `Lease`

## Transition Authority

### Control Plane
May transition:
- all `Job` states
- all `Run` states
- all `Lease` states
- `Agent` lifecycle states
- `Runtime` platform-visible states

### Runtime
May request transitions through protocol calls for:
- progress
- terminal success/failure
- heartbeat-driven lease renewal
- local recovery reporting

Runtime observations do not become authoritative state until accepted by the control plane.

## Agent State Machine

### States
- `provisioning`
- `idle`
- `busy`
- `degraded`
- `draining`
- `terminated`

### Meaning
- `provisioning`
  - control plane has instantiated the durable agent and is preparing execution context
- `idle`
  - agent exists and has no active run
- `busy`
  - agent owns an active run
- `degraded`
  - agent exists but is impaired; may be temporarily excluded from new work
- `draining`
  - agent will accept no new work and is waiting to finish or cancel active work
- `terminated`
  - agent no longer exists as an active execution target

### Legal Transitions
- `provisioning -> idle`
- `idle -> busy`
- `busy -> idle`
- `idle -> degraded`
- `busy -> degraded`
- `degraded -> idle`
- `idle -> draining`
- `busy -> draining`
- `draining -> idle`
- `draining -> terminated`
- `idle -> terminated`

### Illegal Transitions
- `terminated -> any`
- `busy -> provisioning`
- `idle -> provisioning`

## Runtime State Machine

### States
- `registering`
- `idle`
- `busy`
- `degraded`
- `offline`
- `draining`

### Meaning
- `registering`
  - runtime is joining the system
- `idle`
  - runtime is healthy and not currently hosting active work
- `busy`
  - runtime is healthy and has active work
- `degraded`
  - runtime is reachable but impaired
- `offline`
  - runtime is not currently reachable
- `draining`
  - runtime is intentionally excluded from new work

### Legal Transitions
- `registering -> idle`
- `idle -> busy`
- `busy -> idle`
- `idle -> degraded`
- `busy -> degraded`
- `degraded -> idle`
- `idle -> draining`
- `busy -> draining`
- `draining -> idle`
- `idle|busy|degraded|draining -> offline`
- `offline -> idle`

## Job State Machine

### States
- `accepted`
- `queued`
- `running`
- `interrupt_requested`
- `completed`
- `failed`
- `cancelled`
- `blocked`

### Meaning
- `accepted`
  - message was accepted by the control plane
- `queued`
  - job is waiting in an agent or capability queue
- `running`
  - one run currently owns active execution
- `interrupt_requested`
  - cancellation intent has been recorded and is in progress
- `completed`
  - final successful terminal state
- `failed`
  - final unsuccessful terminal state
- `cancelled`
  - final user/operator-interrupted terminal state
- `blocked`
  - not runnable until an external dependency is resolved

### Legal Transitions
- `accepted -> queued`
- `queued -> running`
- `queued -> cancelled`
- `queued -> blocked`
- `blocked -> queued`
- `running -> completed`
- `running -> failed`
- `running -> interrupt_requested`
- `interrupt_requested -> cancelled`
- `running -> queued` only through lease-expiry-driven retry

### Terminal States
- `completed`
- `failed`
- `cancelled`

## Run State Machine

### States
- `created`
- `leased`
- `running`
- `recovering`
- `completed`
- `failed`
- `abandoned`
- `cancelled`

### Meaning
- `created`
  - run record exists, not yet leased
- `leased`
  - control plane granted ownership
- `running`
  - runtime has entered active execution
- `recovering`
  - runtime is performing bounded local recovery inside the same run attempt
- `completed`
  - final successful terminal state
- `failed`
  - final unsuccessful terminal state
- `abandoned`
  - lease expired; ownership lost
- `cancelled`
  - interrupted terminal state

### Legal Transitions
- `created -> leased`
- `leased -> running`
- `running -> recovering`
- `recovering -> running`
- `running -> completed`
- `running -> failed`
- `recovering -> failed`
- `running -> cancelled`
- `leased|running|recovering -> abandoned`

### Terminal States
- `completed`
- `failed`
- `abandoned`
- `cancelled`

## Lease State Machine

### States
- `active`
- `expired`
- `released`

### Meaning
- `active`
  - ownership currently valid
- `expired`
  - ownership lost because heartbeat window was missed
- `released`
  - ownership ended cleanly due to terminal completion/failure/cancellation

### Legal Transitions
- `active -> expired`
- `active -> released`

### Terminal States
- `expired`
- `released`

## Derived State Rules
- A job is `running` if and only if it has one active run in `leased|running|recovering`.
- An agent is `busy` if and only if it hosts one active run.
- A runtime is `busy` if it hosts one or more active runs.
- A run in `abandoned` implies its lease is `expired`.

## Special Rules

### Interrupt
- `queued` job interruption becomes `cancelled` immediately.
- `running` job interruption becomes `interrupt_requested` first, then `cancelled`.
- Interrupt is terminal at job level.

### Retry
- Retry never reopens a terminal run.
- Retry creates a new run and transitions job `running -> queued -> running`.
- Retry is allowed only for retryable failure classes defined in the protocol.

### Teardown
- Tearing down a busy agent causes the active job to end in `cancelled`.
- Tearing down an idle or draining agent transitions agent to `terminated`.
- A draining agent may return to `idle` if draining is lifted and no teardown is requested.

## Illegal Global Invariants
- A job must not have more than one active run.
- A run must not have more than one active lease.
- A terminal job must not return to non-terminal state.
- A terminal run must not return to non-terminal state.
