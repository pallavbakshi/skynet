# AGP Orchestration Surface Specification

## Status
Authoritative

## Purpose
Defines user-facing semantics for `send`, `watch`, `interrupt`, `fetch`, and `handoff`, and what the orchestrator may assume.

## Principle
The orchestrator thinks in agents, messages, jobs, and artifacts. It must not need to reason about queue internals, leases, or runtime recovery.

## `send`
- sends work to a logical agent or capability
- returns either:
  - inline result
  - accepted async with `job_id`

### Orchestrator May Assume
- accepted async means work is durably tracked
- inline result still has durable backing artifacts

### Hidden From Orchestrator
- lease acquisition
- queue redelivery
- runtime-local recovery

## `watch`
- tracks job progress and terminal outcome using ordered events
- may expose status, progress messages, and latest artifacts

## `interrupt`
- requests cancellation of queued or running work
- if queued: immediate cancellation
- if running: cancellation intent recorded, then terminal cancellation

## `fetch`
- retrieves artifact metadata or content using artifact references
- large outputs may be paginated or streamed

## `handoff`
- creates follow-on work from source artifacts
- returns child `job_id`s
- preserves provenance

## Allowed Orchestrator Assumptions
- `job_id` is durable
- terminal state is authoritative
- result artifacts remain retrievable after completion

## Disallowed Orchestrator Assumptions
- exact runtime placement
- exact lease timing
- exact queue backend behavior
- direct visibility into hidden infrastructure/system logs unless explicitly exposed
