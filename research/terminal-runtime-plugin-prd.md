# Product Requirements Document

## Document
AGP Terminal Runtime Plugin PRD

## Version
0.1 Draft

## Purpose
Define the architecture, contracts, lifecycle, and acceptance criteria for AGP terminal-backed runtimes.

This PRD exists to support a robust, swappable runtime model where AGP can supervise agentic CLIs running inside terminal environments such as WezTerm or tmux without coupling core platform behavior to any one terminal host or agent CLI.

The first implementation target is:
- terminal host: `WezTerm`
- agent CLI adapter: `Codex`

This PRD is intentionally broader than that first implementation. It defines the durable plugin boundary AGP will own going forward.

## Problem Statement
AGP Phase 1 requires a real agentic CLI runtime, not just a simulated worker. The current runtime scaffold proves the control loop, but it does not yet supervise a real interactive CLI process.

Using `wezu` directly would be expedient but would hard-code:
- WezTerm behavior
- pane interaction semantics
- output capture assumptions
- recovery behavior

That would create the wrong dependency boundary.

`wezu` should be treated as AGP's first failed attempt at this space:
- useful as reference material
- useful for understanding failure cases and operator ergonomics
- not suitable as the architectural or implementation boundary for AGP

AGP instead needs:
- a terminal host abstraction it owns
- an agent CLI adapter abstraction it owns
- a runtime supervisor that remains independent of any one terminal host or CLI

## Goal
AGP must be able to supervise a real agentic CLI running in a terminal pane, recover from local instability, capture durable artifacts, and remain portable across terminal backends.

## Primary Outcomes
- AGP can run a real Codex CLI session in a WezTerm pane.
- AGP runtime supervision is robust against pane loss, stalled output, local CLI failure, and restart/recovery scenarios.
- AGP core runtime logic does not depend directly on WezTerm or Codex-specific transport semantics.
- Future terminal backends such as tmux can be added by implementing the same plugin contract.

## Non-Goals
- Full session/conversation product model
- Multi-tenant isolation
- Generic remote desktop automation
- Universal support for arbitrary TUIs in V1
- Dynamic plugin loading from third-party packages in V1
- HA control-plane concerns beyond the existing AGP platform

## Scope

### In Scope
- terminal host abstraction
- agent CLI adapter abstraction
- runtime supervisor integration with both abstractions
- per-agent durable terminal session model
- output cursoring and artifact capture
- interrupt and recovery policy
- WezTerm host implementation
- Codex adapter implementation
- tests and failure drills for terminal-backed execution

### Out of Scope
- tmux implementation in the first build
- Claude Code / Gemini adapters in the first build
- browser-based terminal control
- multi-pane group orchestration semantics
- shared live collaborative terminals

## Why This Exists
The runtime must make flaky local execution dependable. That requires AGP to control:
- session identity
- output checkpoints
- completion detection
- interruption policy
- recovery tiers
- artifact capture

These concerns belong to AGP, not to an external helper like `wezu`.

## Architectural Position
This PRD defines a subsystem inside the AGP runtime layer.

The layering is:

1. `AGP Control Plane`
- owns jobs, runs, leases, events, artifacts, retries, and global coordination

2. `AGP Runtime Supervisor`
- owns execution reliability for a claimed run
- interacts with terminal hosts and CLI adapters

3. `Terminal Host Plugin`
- owns terminal session/pane creation, input delivery, output reads, interrupt transport, and session teardown

4. `Agent CLI Adapter`
- owns CLI-specific prompt wrapping, completion detection, failure classification, and output interpretation

The runtime supervisor sits between AGP platform state and the terminal/CLI execution world.

## Core Design Principles

### 1. Pane is Transport, Not Truth
A pane is not the runtime.

The pane only provides:
- execution surface
- input transport
- output transport
- interrupt transport

AGP state remains authoritative.

### 2. Host and CLI Are Separate Concerns
The terminal host and the agent CLI adapter must never be collapsed into a single abstraction.

Examples:
- `WezTermHost` is not `CodexRuntime`
- `CodexAdapter` is not `WezTermPlugin`

### 3. Durable AGP Session Identity
AGP must own a durable logical execution context identity per durable agent.

The terminal host may recreate or replace the underlying pane handle over time, but AGP should preserve a stable logical session binding.

### 4. Output Capture Must Be Cursor-Based
AGP must not rely on ad hoc visible scrollback inspection.

Terminal output reads must use durable AGP-managed cursor/checkpoint semantics so the runtime can:
- read only new output
- recover after restart
- avoid duplicate artifact capture

### 5. Completion Must Be Adapter-Owned
Terminal idleness is not completion.

The adapter must define how completion, failure, and stuck states are recognized for the target CLI.

### 6. Recovery Must Be Tiered
Recovery policy must be explicit and bounded.

The runtime must escalate through increasingly stronger recovery actions rather than jumping immediately to process or pane destruction.

## Technical Vocabulary

### Terminal Session
A durable logical AGP execution context for a specific durable agent. It maps to one current terminal host session handle but may survive host-level recreation.

### Session Handle
A host-specific identifier for the currently attached session or pane.

### Output Cursor
A durable checkpoint that allows the runtime to read only output generated after the checkpoint.

### Terminal Host
A plugin that manages terminal session creation, transport, interrupt, snapshot, and teardown for a specific host implementation.

### Agent CLI Adapter
A plugin that understands a specific agentic CLI’s behavior and decides how to wrap requests, detect completion, classify failures, and extract meaningful outputs.

### Runtime Supervisor
The AGP component that supervises execution for claimed runs using a terminal host and agent adapter.

## Functional Requirements

### 1. Terminal Host Abstraction
AGP must define a terminal host contract that supports:
- creating a new terminal session for an agent
- attaching to an existing terminal session
- confirming whether a session still exists
- sending input text
- optionally submitting input with Enter
- reading output since a cursor
- creating a fresh cursor or mark
- interrupting the current foreground work
- terminating the session
- capturing a snapshot for diagnostics
- reporting session health

### 2. Agent Adapter Abstraction
AGP must define an adapter contract that supports:
- bootstrapping a new CLI session
- formatting a task into CLI input
- deciding whether a run is:
  - still running
  - succeeded
  - failed
  - requires interrupt
  - requires local recovery
- extracting transcript/result/failure content
- classifying recoverable vs non-recoverable local failure conditions

### 3. Runtime Supervisor Behavior
The runtime supervisor must:
- ensure a terminal session exists for the claimed durable agent
- bootstrap the CLI session if needed
- deliver work using the adapter and host
- keep heartbeating while the run is active
- continuously read incremental terminal output
- persist prompt, transcript, exec, result, and failure artifacts
- detect completion using adapter rules, not only terminal idleness
- perform bounded local recovery
- interrupt or fence the local session when required
- release success or failure back to the AGP control plane

### 4. Session Model
Each durable agent must have:
- one logical terminal session identity
- at most one active run at a time
- a stable workspace binding

The host may recreate the backing pane, but AGP must preserve:
- durable agent identity
- logical session identity
- workspace identity
- output cursor progression

### 5. Artifact Model
The runtime must persist:
- `prompt`
- `transcript_log`
- `exec_log`
- `result`
- `failure_evidence`
- session snapshot on terminal failure or recovery exhaustion

Artifacts must be written before terminal success or failure is committed to the control plane.

### 6. Interrupt Semantics
The runtime must support multiple levels of interruption:

1. soft interrupt
- terminal-level interrupt signal for the current CLI interaction

2. session reset
- abandon and re-bootstrap the CLI session within the same logical agent session

3. session recreation
- destroy and recreate the host-level pane/session

4. terminal failure escalation
- fail the run and report back to the control plane

### 7. Recovery Tiers
The runtime must implement bounded escalation:

1. reread output / wait briefly
2. soft interrupt
3. local session reset
4. host session recreation
5. terminal failure escalation to control plane

Every recovery action must:
- emit progress or event metadata
- remain within configured budget
- preserve artifact evidence

## Required Abstractions

### Terminal Host Interface
The AGP-owned interface must support at minimum:

- `create_session(agent_id, workspace_ref, metadata) -> SessionHandle`
- `attach_session(session_id) -> SessionHandle`
- `session_exists(session_id) -> bool`
- `create_cursor(session_id) -> OutputCursor`
- `send_text(session_id, text, enter) -> DeliveryResult`
- `read_output(session_id, cursor) -> OutputReadResult`
- `interrupt(session_id) -> InterruptResult`
- `terminate(session_id) -> TerminateResult`
- `snapshot(session_id) -> SnapshotResult`
- `health(session_id) -> SessionHealth`

The exact Python interface shape may differ, but the capabilities above are mandatory.

### Agent Adapter Interface
The AGP-owned interface must support at minimum:

- `bootstrap_payload(...)`
- `task_payload(message, artifacts, context) -> str`
- `interpret_output(new_output, aggregate_state) -> AdapterDecision`
- `classify_local_failure(error, snapshot, output) -> FailureClassification`
- `extract_artifacts(aggregate_state) -> ArtifactBundle`

### Supervisor Interface
The runtime supervision loop must support:

- `ensure_session(agent)`
- `dispatch(run, job, message)`
- `monitor(run)`
- `recover(run)`
- `finalize_success(run)`
- `finalize_failure(run)`
- `fence(run)`

## WezTerm Host Requirements

### First Host Implementation
The first terminal host plugin is `WezTermHost`.

It must support:
- creating or discovering a pane/session for a durable agent
- identifying the currently active pane handle
- writing text reliably into the pane
- submitting input with Enter
- reading incremental output using AGP-owned cursor semantics
- sending interrupt sequences
- detecting pane disappearance or invalidation
- terminating and recreating the pane

### WezTerm Host Constraints
- AGP must not depend on `wezu` as its architectural boundary
- AGP may borrow implementation ideas from `wezu`
- AGP must own the final host contract, cursoring, and recovery semantics
- AGP may use any WezTerm integration surface it deems appropriate, including:
  - WezTerm CLI commands
  - WezTerm APIs
  - direct host integration patterns exposed by WezTerm
- the implementation choice is subordinate to reliability, debuggability, and swappability
- `wezu` is not required for any part of the final design

### WezTerm Host Implementation Freedom
The first `WezTermHost` implementation is free to use whichever WezTerm interface proves most robust:
- CLI-only
- API-only
- or a hybrid model

AGP should choose the least fragile option that provides:
- reliable session discovery
- deterministic input delivery
- durable output capture
- interrupt support
- diagnosable failure handling

The implementation must not be constrained by compatibility with `wezu`.

## Codex Adapter Requirements

### First CLI Implementation
The first agent CLI adapter is `CodexAdapter`.

It must support:
- initial session bootstrap
- task injection into an already-running Codex CLI context
- completion detection for one AGP run
- failure detection for one AGP run
- transcript extraction
- result extraction
- interrupt compatibility with terminal-host transport

### Codex Adapter Constraints
- completion detection must be deterministic enough for reliable terminal success/failure reporting
- adapter logic must not depend on visible human-only cues alone
- result extraction must preserve raw transcript evidence even if a cleaned result summary is also generated

## Workspace Policy
Phase 1 terminal runtime uses:
- one durable workspace per durable agent
- one durable terminal session per durable agent
- one active run at a time per durable agent

This avoids:
- cross-run workspace ambiguity
- multi-run interleaving in one pane
- unstable session multiplexing

## State and Persistence Requirements
The runtime must persist enough session-side metadata to recover after restart.

Minimum persisted session metadata:
- logical AGP terminal session id
- durable agent id
- current terminal host kind
- current host session handle
- workspace ref
- latest output cursor/checkpoint
- bootstrap status
- last known host health
- recovery counters

This may live in:
- AGP state store
- runtime-local durable store
- or a dedicated runtime-state table

The authoritative design must ensure restarts can continue monitoring or recover cleanly.

## Failure Semantics

### Local Terminal Failures
The runtime must explicitly classify:
- pane disappeared
- pane unreachable
- send failed
- no new output
- corrupted or invalid output cursor
- interrupt failed
- session reset failed
- session recreation failed

### Local CLI Failures
The runtime must explicitly classify:
- CLI crashed
- CLI hung
- CLI produced invalid or partial terminal output
- CLI returned a failure signal
- CLI got into a stale interaction state

### Escalation Rule
If local recovery budget is exhausted, the runtime must:
- capture failure evidence
- mark the run failed or allow lease expiry path according to supervisor policy
- never silently continue in an unknown state

## Configuration Requirements
The runtime must be configurable by:
- host plugin kind
- adapter plugin kind
- workspace root or workspace strategy
- output polling interval
- heartbeat interval
- recovery budgets
- interrupt strategy
- session recreation budget
- artifact capture settings

## Observability Requirements
The runtime must emit structured logs for:
- session creation
- session attach
- task dispatch
- output read
- completion detection
- interrupt
- recovery tier transitions
- pane recreation
- failure escalation

The control plane and operator surfaces must be able to inspect:
- runtime supervision trace for a run
- current host session state
- latest session snapshot on failure

## Security Requirements
- terminal host plugins must not require AGP to leak secrets into logs or artifacts
- adapter prompt wrapping must avoid embedding credentials in transcript artifacts
- session snapshots must be treated as potentially sensitive artifacts

## Testing Requirements

### Unit Tests
- terminal host contract behavior
- adapter completion/failure classification
- cursor progression
- artifact extraction logic
- recovery tier policy

### Integration Tests
- create session -> send -> read -> complete
- claim -> supervise -> heartbeat -> complete
- claim -> supervise -> local failure -> recover -> complete
- claim -> supervise -> local failure -> exhaust recovery -> fail

### Failure Injection Tests
- pane disappears mid-run
- output stops arriving
- interrupt required but first interrupt fails
- session recreation required
- runtime restart while session still exists
- runtime restart while session no longer exists

## Deliverables
- terminal host interface in AGP runtime
- agent adapter interface in AGP runtime
- `WezTermHost`
- `CodexAdapter`
- runtime supervisor integration using both
- terminal runtime tests and failure drills

## Acceptance Criteria
- AGP can execute a real Codex run in a WezTerm pane through the runtime supervisor.
- The terminal host implementation is swappable by interface and does not leak WezTerm-specific assumptions into core runtime logic.
- The runtime can recover from at least:
  - pane disappearance
  - stalled output
  - soft interrupt failure
  - session recreation requirement
- AGP preserves durable artifacts for prompt, transcript, exec log, result, and failure evidence.
- Runtime restart does not invalidate AGP’s ability to recover or continue supervising the durable agent session.
- The implementation does not depend on `wezu` as a required runtime layer.

## Exit Criteria
This PRD is satisfied when AGP owns a robust terminal-runtime plugin architecture and successfully runs Codex in WezTerm as the first production-grade implementation of that architecture.
