# Product Requirements Document

## Document
AGP Plugin CLI PRD

## Version
0.1 Draft

## Purpose
Define a standalone CLI surface for AGP terminal-host plugins and agent-adapter plugins so they can be exercised, debugged, and validated independently of the full AGP control-plane runtime.

This PRD exists to answer:
- how AGP plugin implementations should be exposed as standalone tools
- what users and operators should be able to test without running the full platform
- how to do that without creating a second execution model that drifts from the real runtime path

## Why This Exists
AGP now has a real plugin architecture for terminal-backed runtimes:
- terminal host plugins
- agent adapter plugins
- runtime supervisor integration

That architecture is useful, but it is currently exercised mostly through the AGP runtime and test suite.

This creates practical problems:
- transport bugs are harder to isolate
- adapter bugs are harder to debug
- internal users cannot validate a host or adapter directly
- plugin development is slower because every issue must be reproduced through the full platform

AGP needs standalone plugin CLIs so:
- engineers can debug hosts and adapters directly
- internal users can validate their environment before using orchestration
- plugin contracts are forced to remain clean and testable
- future hosts and adapters can be added without guessing how to verify them

## Goal
Provide a standalone CLI surface over the same host and adapter interfaces used by AGP runtime supervision, so terminal plugins and agent adapters can be used directly for:
- diagnostics
- environment validation
- plugin development
- manual execution
- integration smoke testing

The CLI must be built on the same core Python interfaces already used by AGP, not on a separate implementation path.

## Non-Goals
- building a second orchestration system outside AGP
- exposing control-plane semantics directly in the plugin CLI
- reimplementing job/run/lease logic in standalone tools
- creating host-specific one-off scripts that bypass the shared plugin interfaces
- making the standalone CLI a replacement for AGP orchestration

## Scope
This PRD covers:
- standalone CLI surfaces for terminal hosts
- standalone CLI surfaces for agent adapters
- a thin end-to-end plugin runner for manual execution
- artifact and transcript output for standalone runs
- validation and debugging flows for plugin developers and internal users

This PRD does not cover:
- control-plane APIs
- orchestration semantics
- queue behavior
- lease ownership
- hosted deployment concerns beyond local plugin usability

## User Personas

### 1. Plugin Developer
Needs to:
- create or attach to a terminal session
- send input
- read output
- inspect session health
- test interrupt/recovery behavior
- validate adapter parsing and completion detection

### 2. Internal Operator Or Tester
Needs to:
- verify that a given host backend works on their machine
- verify that a given agent CLI works in that host
- run a small task manually
- inspect transcript, result, and failure artifacts

### 3. AGP Runtime Integrator
Needs to:
- confirm that a new host or adapter obeys the shared contract
- reproduce failures outside the full platform
- compare behavior across hosts like WezTerm and tmux

## Product Principles

### 1. One Core Execution Model
The standalone CLI must use the same Python interfaces as the AGP runtime path:
- `TerminalHost`
- `AgentAdapter`
- shared artifact payload types
- shared plugin factories

The standalone CLI must not create a separate implementation stack.

### 2. Host And Adapter Must Be Independently Testable
A terminal host must be testable without a real agent adapter.
An agent adapter must be testable against a controlled host surface.

### 3. Debuggability Comes First
The standalone CLI is primarily a debugging and validation tool.
Output should be explicit, machine-readable where useful, and suitable for troubleshooting.

### 4. End Users Should Not Need To Know AGP Internals
Users of the standalone CLI may need to know host or adapter names, but they should not need to know:
- leases
- runs
- jobs
- queue state
- fencing tokens

### 5. Host-Specific Behavior Must Stay Behind The Plugin Boundary
WezTerm-specific and tmux-specific details must remain inside host plugins and their CLI parameterization.
The standalone CLI should expose capabilities, not transport implementation details by default.

## Required CLI Surfaces

### A. Host Debug CLI
Provide a CLI surface for direct terminal host interaction.

Examples of intended commands:
- `agp-host list-hosts`
- `agp-host create --host wezterm --agent agt_demo`
- `agp-host attach --host tmux --session ...`
- `agp-host exists --host wezterm --session ...`
- `agp-host health --host tmux --session ...`
- `agp-host send --host wezterm --session ... --text "..."`
- `agp-host read --host tmux --session ...`
- `agp-host snapshot --host wezterm --session ...`
- `agp-host interrupt --host tmux --session ...`
- `agp-host terminate --host wezterm --session ...`

Required capabilities:
- create or discover session
- attach to session
- existence check
- health check
- send text
- read current output
- capture snapshot
- interrupt
- terminate

### B. Adapter Debug CLI
Provide a CLI surface for adapter-specific validation.

Examples of intended commands:
- `agp-adapter list-adapters`
- `agp-adapter bootstrap --adapter codex --host wezterm --session ...`
- `agp-adapter run-once --adapter codex --host tmux --session ... --task "..."`
- `agp-adapter inspect --adapter codex --file transcript.txt`
- `agp-adapter detect --adapter codex --file transcript.txt`

Required capabilities:
- bootstrap adapter into a session
- send a structured task through the adapter
- inspect raw output
- parse completion state
- extract structured result or failure information
- emit artifacts in a normalized shape

### C. Integrated Plugin Runner CLI
Provide a higher-level CLI for manual end-to-end execution using one host and one adapter.

Examples of intended commands:
- `agp-plugin run --host wezterm --adapter codex --agent agt_demo --task "..."`
- `agp-plugin run --host tmux --adapter codex --agent agt_demo --task-file task.md`
- `agp-plugin repl --host wezterm --adapter codex --agent agt_demo`

Required capabilities:
- create or reuse a session
- bootstrap the adapter
- run one task
- collect transcript
- collect result or failure artifacts
- print or save outputs
- optionally keep the session alive for debugging

## Functional Requirements

### 1. Shared Plugin Factory
The standalone CLI must resolve hosts and adapters through the same factory path as AGP runtime supervision.

It must not instantiate host- or adapter-specific code through a parallel codepath that can drift.

### 2. Session Lifecycle Support
The host CLI must support:
- create
- attach
- inspect
- send
- read
- interrupt
- terminate

The session identifier shown to the user must be explicit and reusable across commands.

### 3. Structured Output
The standalone CLI must support:
- human-readable output for normal use
- machine-readable JSON output for debugging and automation

At minimum, commands that inspect or execute should be able to emit JSON.

### 4. Transcript And Artifact Output
Integrated runs must emit:
- transcript log
- exec log when available
- result artifact on success
- failure evidence on failure
- session snapshot on failure when available

The standalone CLI must support:
- printing summaries to stdout
- writing artifacts to disk

### 5. Adapter Execution Contract
The integrated runner must use the adapter’s actual task wrapping and result detection logic.

It must not invent its own completion heuristics separate from the adapter implementation.

### 6. Interrupt And Failure Handling
The host CLI and integrated runner must support:
- soft interrupt
- session termination
- surfaced host failure
- surfaced adapter failure

Failures must be distinguishable as:
- host transport failure
- adapter execution failure
- invalid output or incomplete result

### 7. Reusability Across Hosts
The same standalone CLI surface must work for:
- `wezterm`
- `tmux`
- future tmux-like hosts

Host-specific flags are allowed where necessary, but the command model must remain stable.

### 8. Reusability Across Adapters
The same standalone CLI surface must work for:
- `codex`
- future adapters like `claude-code`

Adapter-specific flags are allowed, but the command model must remain stable.

## UX Requirements

### 1. Clear Separation Of Levels
The product should expose three levels:
- low-level host debugging
- adapter debugging
- high-level integrated run

These should be separate command groups, not one overloaded command.

### 2. Minimal Surprise
If a command mutates host state, that should be obvious.
If a command only reads state, it should not create or destroy sessions implicitly unless explicitly requested.

### 3. Explicit Session Visibility
Users must be able to see:
- host kind
- adapter kind where relevant
- session id
- agent id when relevant
- workspace or cwd when available
- health status where supported

### 4. Debuggable Failure Messages
If a host command fails, the output must say whether the failure came from:
- host backend invocation
- missing session
- unsupported capability
- adapter parse failure
- underlying CLI output contract mismatch

## Architecture Requirements

### 1. No Backward-Compatibility Shim Layer
This feature should be built on the current plugin architecture directly.

Do not introduce a second abstraction or temporary compatibility wrapper solely to preserve older runtime codepaths.

### 2. Shared Library, Multiple Frontends
The implementation should be:
- one shared plugin library
- AGP runtime supervisor integration
- standalone CLI frontend

Not:
- one runtime implementation
- one separate standalone implementation

### 3. Stable Plugin Capability Surface
Hosts should expose a stable minimum interface that standalone CLIs can depend on:
- session create or attach
- existence
- health
- send
- read
- snapshot
- interrupt
- terminate

Adapters should expose a stable minimum interface:
- bootstrap
- run task
- interpret output
- produce structured artifacts

### 4. File And Artifact Handling
Standalone runs must write artifacts through shared artifact helper logic where practical, or through a normalized local-output writer that matches AGP artifact conventions closely enough for debugging.

The standalone CLI must not invent unrelated artifact naming semantics.

## Security Requirements
- the standalone CLI must not print secrets by default
- host commands must avoid leaking credentials in logs or artifact outputs
- adapter outputs must redact or avoid sensitive bootstrap details where appropriate
- machine-readable output must not include hidden secret fields unless explicitly requested in a secure debug mode

## Observability Requirements
- standalone commands should support verbose/debug output
- integrated runs should emit timing and outcome information
- failures should preserve enough context for bug reports:
- host kind
- adapter kind
- session id
- failure type
- raw or cleaned transcript pointers

## Acceptance Criteria
- A user can validate `wezterm` host behavior without running the control plane.
- A user can validate `tmux` host behavior without running the control plane.
- A user can run one Codex task end to end against either supported host through a standalone CLI.
- The standalone CLI uses the same host and adapter implementations as AGP runtime supervision.
- Host failures, adapter failures, and invalid output are distinguishable in CLI output.
- Transcripts and result or failure artifacts from standalone runs are inspectable and reproducible.
- Adding a new host or adapter requires implementing the shared interface, not inventing a new CLI shape.

## Test Strategy
- unit tests for host CLI command wiring
- unit tests for adapter CLI command wiring
- integrated tests for one-task execution through the standalone runner
- snapshot tests for JSON output shape
- regression tests for interrupt and failure handling
- cross-host tests ensuring the same CLI surfaces work for both WezTerm and tmux

## Exit Criteria
This feature is complete when:
- AGP plugins are usable directly through standalone CLI tools
- the standalone tools are built on the same shared plugin interfaces as the runtime
- internal users can validate hosts and adapters without invoking the full AGP platform
- plugin developers can debug transport and adapter behavior independently of orchestration
