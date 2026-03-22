# Product Requirements Document

## Document
Queue, Runtime, And Testability Refactor PRD

## Version
0.1 Draft

## Purpose
Finish the remaining platform refactor by cleaning up backend ownership semantics, removing fragile global state, and reorganizing tests around stable service seams.

This document exists to answer:
- how queue backends become structurally coherent
- how runtime-side leftovers and weak abstractions are cleaned up
- how AGP’s test suite stops relying so heavily on oversized end-to-end files

## Why This Exists

After modularizing the control plane and hardening schema authority, AGP will still have one major class of technical debt left:
- queue backends with mixed authority and shadow state
- runtime supervisor cleanup and production-code leftovers
- oversized integration tests that make refactors expensive to verify

These are not cosmetic issues.

They directly affect:
- operational correctness in Redis-backed queue mode
- confidence in distributed behavior changes
- how quickly future runtime and orchestration features can be delivered safely

## Goal

Deliver a coherent backend and testability architecture that:
- makes queue ownership semantics explicit
- removes fragile production code leftovers
- reduces reliance on giant scenario tests as the only protection against regressions
- preserves current runtime behavior while making it easier to change safely

## Non-Goals

- replacing the runtime plugin model
- redesigning tmux / wezterm integration
- changing end-user job dispatch semantics
- replacing all integration tests with unit tests

## Scope

This PRD covers:
- queue backend contract cleanup
- Redis backend consistency model
- removal of unsafe assertions and stray production leftovers
- runtime supervisor cleanup
- targeted test suite decomposition

This PRD does not cover:
- initial control-plane module splitting
- migration framework adoption
- major feature additions

## Current State

### Strengths To Preserve
- AGP supports multiple queue backends
- runtime claim / heartbeat / complete / fail flow is proven in real environments
- end-to-end tests already cover meaningful flows

### Weaknesses To Fix
- Redis queue mode maintains both Redis transport state and SQL shadow state without a clean authority model
- module-level backend singletons remain in place
- runtime supervisor still contains stub / leftover behavior
- test coverage is skewed toward large integration files

## Problem Statement

The platform now has enough behavior that implementation seams matter as much as features.

Without cleaning up backend ownership and test structure:
- Redis-backed behavior will remain harder to reason about than DB-backed behavior
- runtime refactors will continue to surface failures late
- large-file integration tests will remain the only practical safety net for too many changes

## Target State

### Queue Backend Ownership

Every queue backend has a clear contract for:
- source of truth
- durability expectations
- reconciliation behavior
- operator visibility model

For Redis specifically, AGP must choose one of:
1. Redis as authoritative transport state, with SQL as rebuildable projection
2. SQL as authoritative delivery state, with Redis as acceleration transport

The implementation must not continue as an ambiguous hybrid.

### Runtime Supervisor Cleanliness

The runtime supervisor must not contain:
- debug leftovers like `sleep(0)`
- production `assert` statements for runtime invariants
- effectively empty cleanup hooks that imply safety without providing it

### Testability

Tests should align to architectural seams:
- service tests
- backend contract tests
- route / API tests
- end-to-end scenario tests

End-to-end tests remain, but they should stop carrying most of the burden alone.

## Refactor Areas

### Area 1: Queue Backend Contract Clarification

Current state:
- DB, delivery-table, in-memory, and Redis backends exist
- Redis keeps both Redis-native state and SQL delivery rows

Required change:
- define a formal backend contract that distinguishes:
  - transport state
  - durable visibility state
  - redrive and dead-letter ownership

Acceptance criteria:
- queue backend implementations follow the same conceptual contract
- Redis behavior is documented and test-backed rather than inferred from code

### Area 2: Redis Backend Consistency Simplification

Current state:
- Redis and DB are both mutated in one backend path

Required change:
- choose and implement one authority model
- ensure redrive and dead-letter behavior are derived from that model

Acceptance criteria:
- crashes between backend operations do not leave undefined ownership semantics
- operator-visible queue inspection remains available

### Area 3: Runtime Supervisor Cleanup

Current state:
- `_cleanup_workspace(...)` is effectively a stub
- `sleep(0)` is still present
- runtime-related work has some circular import pressure

Required change:
- either implement meaningful workspace cleanup semantics or make the hook explicitly no-op and rename/document it accordingly
- remove stray debug leftovers
- replace production `assert` usage with explicit error handling
- simplify runtime module dependency edges where possible

Acceptance criteria:
- runtime supervisor logic is explicit about what cleanup is and is not guaranteed
- no production behavior depends on `assert`
- runtime module imports are easier to follow and test

### Area 4: Test Suite Recomposition

Current state:
- [test_mvp_flow.py](/home/user/projects/skynet/tests/test_mvp_flow.py) is 5015 lines
- key logic is validated mostly through large scenario tests

Required change:
- split large tests by domain:
  - dispatch / queue
  - leases / recovery
  - artifacts / finalization
  - handoff / provenance
  - runtime supervision
  - operator surfaces
- add backend contract tests, especially for Redis and artifact store implementations

Acceptance criteria:
- changes to queue or runtime internals can be validated with focused tests before running giant scenario files
- Redis backend and artifact store behaviors have direct integration coverage

## Test Strategy

### Layer 1: Service And Backend Unit Tests
- queue transitions
- run / lease transitions
- backend-specific edge cases

### Layer 2: API Integration Tests
- route contracts
- auth behavior
- response schema correctness

### Layer 3: End-To-End Scenarios
- retain a smaller set of comprehensive multi-component flows
- focus them on proving real distributed behavior, not every edge case

## Risks

### Risk 1: Queue Refactor Can Introduce Delivery Regressions
- mitigation: preserve external semantics, add backend contract tests first

### Risk 2: Shrinking Big Tests Too Early
- mitigation: do not delete scenario coverage until focused tests are green

### Risk 3: Runtime Cleanup Semantics Become Overengineered
- mitigation: make explicit guarantees, not speculative safety features

## Deliverables

- explicit queue backend contract documentation in code
- Redis backend refactor with clear authority semantics
- cleaned-up runtime supervisor and related runtime abstractions
- decomposed test suite with backend-specific coverage

## Success Criteria

This PRD is complete when:
- queue backend behavior is explainable in one page without hand-waving
- runtime supervision code has no misleading leftovers
- refactors in queue or runtime code can be validated through focused tests rather than only giant scenario files
