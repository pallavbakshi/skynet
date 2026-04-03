# AGP CLI Reliability, Observability, And Structured Output Hardening

## Objective

Make AGP CLI trustworthy for multi-agent troubleshooting by removing the need for raw `tmux`, ad hoc API inspection, and manual residue cleanup during normal operation.

## Problem Statement

Current AGP workflows are powerful but leak implementation details under stress:

- Long-lived runtimes are awkward to manage outside persistent terminals.
- `agp review` becomes opaque when a reviewer runtime misbehaves.
- Structured-output jobs can succeed in-pane but fail at the platform layer due to noisy terminal capture.
- Attachment/temp residue creates operator noise.
- Failure messages often report the validation failure but not the underlying adapter/runtime cause.

## Goals

- Make structured-output delivery reliable across adapters.
- Make runtime/reviewer state inspectable from AGP CLI alone.
- Make review loops self-diagnosing.
- Reduce operator dependence on `tmux`, raw HTTP, and local cleanup.
- Preserve AGP's current lightweight local-dev workflow.

## Non-Goals

- Full daemon/orchestrator deployment system.
- Web UI.
- SSE/WebSocket live event streaming.
- Centralized distributed logging stack.
- Fully autonomous runtime restart/remediation policies.

## Users

- Primary: single-operator developers running local CP + runtimes.
- Secondary: engineers debugging multi-agent review/fix loops.
- Tertiary: future remote operators running multiple runtimes across machines.

## Success Metrics

- 90% reduction in cases where operator must use `tmux capture-pane` for diagnosis.
- Output-contract reviewer jobs succeed when valid JSON was produced, even if pane output contains extra TUI noise.
- `agp review` failures identify root cause category in one screen.
- No persistent `agp-attachments/`-style residue after successful runs.
- Fresh-stack end-to-end review tests pass for Codex and Claude paths.

## Core Requirements

### 1. Structured Result Delivery

AGP must treat file-based result delivery as the primary path for output-contract jobs.

- Every output-contract adapter run gets a deterministic temp result file path.
- Prompt contract instructs model to write only the JSON object to that file.
- Runtime reads file first, validates it, and uses it as authoritative result.
- Terminal extraction remains fallback only.
- Fallback extraction must recover the last valid top-level JSON object from noisy output.
- If file delivery fails and fallback succeeds, runtime records a warning artifact/diagnostic note.
- If both fail, error must distinguish:
  - no result file produced
  - invalid file JSON
  - valid JSON candidate found in pane but not selected
  - no JSON candidate found

### 2. Shared Adapter Output Pipeline

Codex and Claude must use a shared structured-output helper.

- Common helper responsibilities:
  - result file path generation
  - file safety checks
  - JSON extraction from noisy terminal text
  - contract-aware candidate selection
  - normalized diagnostics
- Adapter-specific code should only provide terminal text sources and lifecycle hooks.
- Eliminate divergent JSON salvage logic across adapters.

### 3. Runtime Diagnostics CLI

Add first-class diagnostic commands.

- `agp health`
- `agp diagnose runtime <runtime-id>`
- `agp diagnose agent <agent-id>`

Required output:

- runtime registration status
- bound agent(s)
- current/last job id
- heartbeat age and miss indicators
- terminal host kind
- session existence
- foreground TUI/process liveness
- last meaningful runtime log entries
- last known pane tail / cleaned output summary when available
- recommended next operator action

### 4. Review Loop Status And Diagnosis

Make `agp review` transparent.

- Show round number, active phase, reviewer job id, dev job id, elapsed seconds.
- Show current reviewer runtime id and health summary.
- On repeated polling, emit structured status instead of generic "working".
- Add `agp review-status <source-job-id>`.
- Add `agp review-diagnose <source-job-id>`.

Review diagnosis must include:

- current phase
- active job states
- last verdict if any
- latest runtime heartbeat
- whether reviewer pane appears idle/working/stuck/completed
- output-contract validation status
- whether usable JSON candidate was found

### 5. Failure Reporting Improvements

Surface the right abstraction.

Instead of only:

- `result artifact is not valid JSON`

Also include:

- adapter completed or not
- file result present or absent
- JSON candidate found in terminal output or not
- runtime alive or not
- likely root-cause category:
  - adapter extraction failure
  - model contract violation
  - runtime bookkeeping failure
  - terminal session failure
  - control-plane validation failure

### 6. Attachment And Temp Artifact Hygiene

Temporary operator artifacts must be managed automatically.

- Replace ad hoc `agp-attachments/` with AGP-owned temp workspace, e.g. `.agp-tmp/attachments/`.
- Cleanup on successful completion.
- Cleanup stale temp dirs on next startup.
- Add `agp cleanup`.
- Add `--keep-temp-artifacts` for debugging.

### 7. Runtime Lifecycle Improvements

Improve local operator ergonomics without overbuilding.

- Add AGP-managed detached lifecycle commands:
  - `agp runtime start`
  - `agp runtime stop`
  - `agp runtime status`
  - `agp stack up`
  - `agp stack down`
  - `agp stack status`
- These should manage PID files, logs, and health checks.
- Keep Makefile wrappers, but make them thin wrappers around AGP lifecycle commands.

### 8. Reviewer Mode Controls

Review/advice tasks need bounded behavior.

- Add reviewer execution hints:
  - no-tools
  - max-tool-calls
  - max-runtime-seconds
  - no-repo-exploration
  - answer-from-prompt-only
- `agp review` should support a lightweight "advice mode" for product/UX prompts that should not trigger full repo spelunking.

## User Stories

- As an operator, I can see why a reviewer job failed without opening tmux.
- As an operator, I can trust output-contract jobs if the model produced valid JSON anywhere recoverable.
- As an operator, I can restart CP/runtimes and re-run a review loop from AGP commands alone.
- As a developer, I can compare reviewer/dev/runtime state from one CLI status view.
- As a maintainer, I can add a new adapter without re-implementing structured-output recovery.

## Functional Specs

### Structured Output

- File path is created per run.
- File path is private and ownership-checked.
- Result helper returns:
  - selected payload
  - source: file|visible|scrollback|fallback
  - diagnostics metadata
- Diagnostics are attachable as artifacts for failed jobs.

### Diagnostics

- New API surface may be CLI-only first, but should be backed by reusable runtime inspection helpers.
- Diagnostics should work for local and tmux-backed runtimes.
- Diagnostics must degrade gracefully when runtime is offline.

### Review Status

- Persist review session state as today, but make it queryable from dedicated commands.
- Include reviewer runtime metadata in saved status when known.

## Implementation Phases

### Phase 1: Reliability

- Shared structured-output helper.
- File-first result delivery for Claude and Codex.
- Improved validation/failure messages.
- Regression tests for noisy-output reviewer jobs.

### Phase 2: Observability

- `agp health`
- `agp diagnose runtime`
- `agp review-status`
- `agp review-diagnose`
- Structured polling output in `agp review`

### Phase 3: Hygiene And Lifecycle

- temp artifact cleanup
- `agp cleanup`
- detached runtime/stack lifecycle commands
- Makefile wrapper alignment

### Phase 4: Behavior Controls

- reviewer/advice mode constraints
- no-tools / bounded-tools review prompts
- execution-mode selection by task class

## Acceptance Criteria

- Claude reviewer JSON-contract job succeeds on fresh restarted stack even with mixed TUI noise.
- Codex reviewer JSON-contract job still succeeds.
- Failed structured-output jobs report root cause with adapter/runtime context.
- `agp review-status` shows current round/phase/job ids.
- `agp diagnose runtime <id>` replaces the need for manual pane capture in common debugging paths.
- No temp attachment residue remains after successful runs.
- Restarting local stack and runtimes can be done without keeping foreground shells open.

## Test Plan

- Unit tests:
  - JSON extraction from noisy output
  - file-first result selection
  - fallback selection precedence
  - temp cleanup behavior
- Integration tests:
  - fresh CP + fresh Claude reviewer + output-contract review success
  - fresh CP + fresh Codex reviewer + output-contract review success
  - review loop stuck/runtime-dead diagnostics
  - runtime lifecycle detached start/stop/status
- Regression tests:
  - valid JSON with trailing noise
  - valid JSON in pane but missing file
  - missing file and invalid pane JSON
  - queued/running/completed review-session status reporting

## Risks

- Overfitting extraction logic to current TUI shapes.
- Adding too much lifecycle abstraction before diagnostics are stable.
- Mixing operator UX improvements with deeper runtime semantics in one rollout.

## What Not To Build Yet

- Auto-restart/self-healing runtimes.
- Real-time streaming event transport.
- Full service manager integration.
- Rich centralized telemetry stack.
- Web dashboard.

## Recommended First Milestone

Ship one milestone containing:

- shared file-first structured result delivery
- Claude/Codex unified fallback extraction
- improved error messages
- `agp health`
- `agp review-status`

That is the minimum set that materially changes operator trust.
