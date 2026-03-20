# Product Requirements Document

## Document
Distributed Terminal Runtime Gap PRD

## Version
0.1 Draft

## Purpose
Define the gap between AGP's current implementation and the target system where an orchestrator can reliably use multiple co-agents running in terminal panes across multiple computers.

This document is not a general architecture PRD. It is a delivery-gap PRD.

It answers:
- what AGP can already do
- what AGP cannot honestly claim yet
- what must be built to reach the first real distributed pane-backed multi-agent deployment

## Scenario
Target scenario:
- one orchestrator
- three logical co-agents
- three different terminal panes
- three different computers
- each pane running a real agentic CLI such as Codex

Example:
- `agt_research` on machine A
- `agt_backend` on machine B
- `agt_review` on machine C

Each machine runs a runtime process.
Each runtime supervises a real terminal-backed agent session.
The orchestrator sends work to logical agents, not to panes.

## Current State Summary

### What Works Today
AGP already supports the core distributed platform loop:

- durable agents
- messages, jobs, runs, leases
- remote runtime registration over HTTP
- queueing and claim flow
- heartbeats, interrupts, retries, recovery
- artifacts and event streams
- operator observability surfaces
- security/auth basics
- runtime plugin architecture

These parts are real and implemented.

### What Exists But Is Not Fully Real Yet
AGP now has the correct runtime plugin abstractions:

- `TerminalHost`
- `AgentAdapter`
- `RuntimeSupervisor`

It also has first concrete implementations:

- `WezTermHost`
- `CodexAdapter`

However, these are not yet enough to claim that AGP fully supervises real Codex sessions across real panes on real machines in production conditions.

## Problem Statement
The control-plane and runtime platform layers are substantially implemented, but the last mile of real terminal-backed execution is still incomplete.

As a result, AGP can currently claim:
- distributed logical orchestration
- remote runtime ownership and recovery
- terminal plugin architecture

But it cannot yet fully claim:
- robust live Codex execution in real WezTerm panes
- reliable long-running pane supervision across machines
- production-grade pane-backed multi-agent operation

The current gap is no longer architectural ambiguity.
The current gap is operational and execution realism.

## Goal
Close the gap so AGP can reliably run a real orchestrated multi-agent workload where:

- the orchestrator addresses logical agents
- each logical agent is backed by a real CLI session in a terminal pane
- those panes live on different machines
- AGP supervises the sessions end to end
- AGP survives local pane and CLI failures without losing global control-plane correctness

## Non-Goals
- redesigning the control plane
- replacing the current agent/job/run/lease model
- changing the orchestration abstraction to expose panes directly
- supporting every terminal host or every agentic CLI in V1
- solving HA control-plane scale-out beyond the already documented platform roadmap

## Target User Experience
The orchestrator should be able to:

1. address logical agents:
- `agt_research`
- `agt_backend`
- `agt_review`

2. send work without caring about pane mechanics

3. track job progress and artifacts

4. interrupt or retry work when needed

5. rely on AGP to manage:
- pane identity
- session health
- CLI transport
- output capture
- recovery and fencing

The orchestrator should not need to know:
- pane ids
- WezTerm handles
- tmux window ids
- control sequences
- output cursor internals

## Current Capabilities

### Platform Capabilities Already Present
- control-plane APIs for jobs, runs, artifacts, agents, runtimes, handoffs, and events
- runtime claim / heartbeat / complete / fail / cancel flow
- queue backend abstraction with delivery and redrive semantics
- artifact-store abstraction with multiple backends
- alerting, logs, and job trace surfaces
- background sweeps for lease expiry, stale runtimes, idle agents, and draining transitions
- RBAC and token rotation basics

### Runtime Abstractions Already Present
- terminal host abstraction
- agent adapter abstraction
- runtime supervisor abstraction
- WezTerm host implementation
- Codex adapter implementation
- in-process terminal host used for tests

### What This Means
The system already has the correct control-plane shape and most of the runtime control loop.

The remaining work is not about inventing the model.
It is about making the live terminal-backed execution path robust enough to trust.

## Delivery Gaps

### Gap 1: Live Codex-on-WezTerm Execution Is Not Yet Proven
Current state:
- `WezTermHost` exists
- `CodexAdapter` exists
- both are covered by fake-runner and synthetic tests

Missing:
- a real runtime mode that drives an actual Codex CLI in an actual WezTerm pane
- live validation that the runtime can bootstrap, dispatch, detect completion, detect failure, and finalize a real Codex session

Why this matters:
- until this exists, AGP is still proving the runtime contract mostly in simulation

### Gap 2: Output Checkpointing Is Not Durable Enough For Long-Lived Sessions
Current state:
- AGP owns cursor semantics
- `WezTermHost` can read incremental output

Missing:
- a durable long-session output checkpoint model beyond a bounded scrollback read window
- safe handling when output volume exceeds the current capture window
- restart-safe continuation of output capture for long-running pane sessions

Why this matters:
- pane-backed execution becomes unreliable if output cursoring silently drops context or re-reads stale output

### Gap 3: Codex Adapter Contract Needs Live Hardening
Current state:
- Codex runs now use a run-scoped envelope
- results are correlated to `run_id`

Missing:
- live validation that Codex can reliably follow the AGP run envelope
- prompt/bootstrap strategy hardened against real Codex behavior
- documented fallback behavior when Codex does not emit the expected terminal line

Why this matters:
- the adapter is structurally sound, but real CLI behavior can still invalidate naive assumptions

### Gap 4: Runtime Supervision Is Not Yet Managing A Real Foreground CLI Process
Current state:
- supervisor controls session lifecycle abstractly
- recovery, heartbeat, and failure reporting exist

Missing:
- verified supervision of a real interactive CLI process inside the pane
- stronger handling of:
  - CLI wedges
  - prompt delivery problems
  - pane disappearing mid-run
  - interrupted or half-completed terminal interactions

Why this matters:
- this is the actual difference between a plausible runtime and a dependable runtime

### Gap 5: Multi-Machine Live Validation Has Not Happened
Current state:
- distributed runtime model exists in architecture and code
- local deployment assets exist

Missing:
- an end-to-end live validation where:
  - three runtimes register from three computers
  - three durable agents are backed by three real panes
  - the orchestrator dispatches real work to all three
  - the system demonstrates success, failure handling, and interrupt behavior

Why this matters:
- without a live distributed validation, the target scenario remains inferred rather than demonstrated

### Gap 6: Production-Grade Host Diversity Is Still Future Work
Current state:
- plugin architecture is clean
- WezTerm is the first host

Missing:
- second host implementation such as `tmux`
- proof that the host boundary is truly swappable in practice, not just in code structure

Why this matters:
- the plugin model is only proven once at least one alternative host can be added without runtime redesign

## Out-of-Scope Gaps For This Document
These remain important, but they are not the primary blockers for the first real distributed pane-backed demo:

- HA control-plane deployment
- production database migration off SQLite
- full metrics/dashboard pipeline
- cluster-scale deployment automation
- broad multi-tenant security isolation

Those belong to broader platform and Phase 3 work, not to this specific gap closure.

## Required Deliverables

### Deliverable 1: Live WezTerm Runtime Path
AGP must be able to run:
- `RuntimeSupervisor`
- `WezTermHost`
- `CodexAdapter`

in one live execution path against a real WezTerm pane.

Minimum expectations:
- create or find pane
- bootstrap Codex
- dispatch one run
- capture output incrementally
- finalize success or failure

### Deliverable 2: Durable Output Checkpoint Strategy
AGP must define and implement a more durable output checkpointing model for pane sessions.

Minimum expectations:
- not limited to an unsafe short scrollback window
- can continue across repeated reads
- can survive runtime restart in a bounded, diagnosable way

### Deliverable 3: Real Codex Adapter Validation
AGP must validate that the Codex adapter works against a real Codex CLI.

Minimum expectations:
- run-scoped envelope accepted by Codex
- success detection works
- failure detection works
- malformed or missing terminal payload causes bounded recovery or failure

### Deliverable 4: Multi-Machine Demonstration
AGP must demonstrate the target scenario on three machines.

Minimum expectations:
- three runtimes register
- three durable agents are provisioned
- each agent binds to its own pane-backed session
- orchestrator dispatches work to all three
- artifacts and results are collected

### Deliverable 5: Recovery Demonstration For Live Pane Execution
AGP must prove at least these live runtime recovery cases:
- pane disappears during a run
- Codex does not emit terminal result within budget
- interrupt is requested while Codex is active

## Functional Requirements

### Runtime Dispatch
The runtime must:
- claim a run
- attach to the durable agent session
- deliver run-scoped Codex input
- keep heartbeating during execution
- finalize only after durable artifact write

### Runtime Recovery
The runtime must:
- detect missing pane/session
- detect missing terminal result for active run
- attempt bounded local recovery
- recreate the pane when required
- preserve diagnostic artifacts on local failure

### Output Interpretation
The adapter must:
- ignore terminal output for other runs
- ignore stale output from previous sessions
- only complete/fail the current run when a valid run-scoped terminal payload is observed

### Orchestrator Abstraction
The orchestrator must continue to interact only with:
- agents
- jobs
- artifacts
- status

Pane-specific diagnostics may be exposed to operators, but not required for orchestration.

## Acceptance Criteria

AGP can claim this gap is closed when all of the following are true:

1. one real Codex run completes through:
- `RuntimeSupervisor`
- `WezTermHost`
- `CodexAdapter`

2. one real Codex run fails cleanly and produces:
- transcript log
- exec log
- failure evidence
- session snapshot

3. one interrupted live run transitions cleanly through AGP cancellation semantics

4. one pane-loss scenario is recovered or failed in a bounded and diagnosable way

5. a three-runtime, three-agent, three-machine orchestrated run completes end to end with artifacts and results available through the control plane

## Risks

### Real CLI Behavior May Diverge From Assumptions
Codex may not consistently obey the AGP run envelope without prompt hardening or adapter changes.

### Terminal Output Semantics May Be Fragile
WezTerm output reads may require more durable host-specific handling than the current bounded scrollback approach.

### Recovery May Need Host-Specific Rules
Pane recreation and interrupt behavior may differ materially between WezTerm and future tmux support.

## Recommendations

### Immediate Next Step
Implement the first live execution path:
- one runtime
- one WezTerm pane
- one Codex session
- one real AGP job

### Next Step After That
Harden output checkpointing for long-lived sessions.

### Next Step After That
Run the first three-machine orchestrated demo.

## Exit Condition
This document is complete when AGP can truthfully say:

"The orchestrator can dispatch work to multiple logical agents, each backed by a real terminal-hosted CLI session on different machines, and AGP reliably supervises those sessions end to end."
