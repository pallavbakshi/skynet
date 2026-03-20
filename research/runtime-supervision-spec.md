# AGP Runtime Supervision Specification

## Status
Authoritative

## Purpose
Defines runtime responsibilities, local recovery, CLI crash handling, workspace cleanup, fencing handoff, heartbeat emission, and artifact upload behavior.

## Runtime Responsibilities
- register with control plane
- claim eligible work
- host durable agents
- execute agentic CLI sessions
- emit heartbeats
- upload artifacts
- attempt bounded local recovery
- cooperate with fencing and interruption

## Local Recovery Loop
The runtime may locally recover without creating a new run for:
- transient prompt delivery failure
- temporary CLI crash with recoverable local context
- output capture failure
- local workspace contamination detected before terminalization

## Local Recovery Budget
- bounded by count and elapsed-time policy
- exhaustion escalates to control-plane-visible failure or lease expiry outcome

## CLI Crash Handling
- detect process exit or TUI death
- decide recoverable vs non-recoverable locally
- if recoverable, restart local execution context and continue same run in `recovering`
- if not recoverable, either:
  - report failure if still holding valid lease
  - or allow lease expiry / abandonment path

## Workspace Cleanup
Cleanup required after:
- cancellation
- force teardown
- fatal local corruption
- policy-driven post-completion cleanup

Cleanup should remove:
- temp files
- stale locks
- ephemeral credentials
- local daemon/session residue

## Fencing Handoff
- On lease expiry or explicit fencing request, runtime must attempt to kill the owned local execution context.
- Runtime must treat fenced runs as no longer authoritative.
- Runtime must stop heartbeating and stop attempting terminal reports for fenced runs.

## Heartbeat Emission
- runtime emits heartbeat for each active lease at fixed interval
- heartbeat is independent of progress
- missed heartbeat budget is three consecutive intervals by default

## Artifact Upload Behavior
- runtime uploads required artifacts before terminal success/failure report
- uploads must produce artifact references and integrity metadata
- artifact upload failure is not terminal success; runtime must not finalize job until upload succeeds

## Interrupt Handling
- runtime must respond to interrupt intent by halting local execution
- if interrupt succeeds under a valid lease, runtime transitions via cancellation path
- if runtime loses lease before handling interrupt, control plane remains authoritative

## Runtime Invariants
- one active run per durable agent
- no terminal report without valid lease and fencing token
- no artifact finalization after fencing invalidation
