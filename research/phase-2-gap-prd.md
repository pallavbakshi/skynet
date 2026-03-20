# Product Requirements Document

## Document
AGP Phase 2 Gap PRD

## Version
0.1 Draft

## Purpose
Define the gap between AGP's current implementation and the target defined in the Phase 2 Technical PRD.

This document is not a replacement for the Phase 2 Technical PRD. It is a delivery-gap document that answers:
- what Phase 2 capabilities are already implemented
- what Phase 2 capabilities are only partially implemented
- what is still missing before Phase 2 can be considered complete

## Scope
This document evaluates the current codebase against:
- [Phase 2 Technical PRD](/home/user/projects/skynet/research/phase-2-technical-prd.md)

It focuses on:
- control-plane behavior
- data model coverage
- runtime coordination
- orchestration and operator surfaces
- test and acceptance coverage

It does not attempt to restate Phase 3 infrastructure goals unless those are incorrectly assumed by the Phase 2 implementation.

## Summary
Phase 2 is substantially implemented.

AGP already behaves like a real multi-runtime platform in several important ways:
- multiple runtimes can register and claim work
- durable agents survive runtime churn
- capability-based routing exists
- handoff exists
- queue delivery, redrive, and dead-letter inspection exist
- runtime health and stale-runtime sweep behavior exist
- inspection APIs for jobs, events, artifacts, agents, runtimes, queue deliveries, traces, and logs exist

However, Phase 2 is not complete yet.

The remaining gaps are not architectural in the broad sense. They are concentrated in:
- deterministic routing policy
- provenance validation
- operator inspection depth
- missing persisted platform metadata
- large-artifact retrieval semantics
- coverage of the Phase 2 test strategy

## Current State

### Implemented Phase 2 Areas
- distributed runtime registration and claim flow
- runtime heartbeat, lease expiry, fencing-token validation, and reassignment attempts
- agent identity independent of runtime replacement
- capability-targeted and agent-targeted queueing
- handoff creation backed by new jobs and artifact references
- machine-readable list, watch, and inspection APIs
- queue transport abstraction with delivery, redrive, and dead-letter semantics
- runtime health classification at the current `status` and `health_status` level
- operator surfaces for stale runtimes, queue deliveries, traces, alerts, logs, and artifacts

### Phase 2 Areas That Exist But Are Weaker Than The PRD
- capability-pool routing policy
- handoff validation and provenance rigor
- runtime claimed-work inspection
- artifact retrieval for large outputs
- explicit persisted health history
- explicit persisted capability-pool metadata
- test coverage for some distributed failure modes

## Problem Statement
The implementation has most of the visible Phase 2 surfaces, but some of the deeper guarantees from the Phase 2 PRD are still either:
- implied rather than explicit
- implemented operationally but not modeled durably
- present in part of the platform but not exposed in the required inspection shape
- covered by behavior but not locked down by tests

Because of this, AGP is close to Phase 2 completion, but it cannot yet honestly claim full conformance to the Phase 2 PRD.

## Goal
Close the remaining gaps so AGP can be considered a coherent Phase 2 multi-runtime platform with:
- deterministic routing semantics
- validated handoff provenance
- stronger operator inspection
- complete Phase 2 data-model coverage where required
- artifact retrieval semantics suitable for large outputs
- explicit test evidence for the remaining distributed guarantees

## Non-Goals
- solving Phase 3 HA deployment
- multi-region or multi-tenant isolation
- redesigning the job / run / lease protocol
- replacing the runtime plugin model
- introducing new orchestration abstractions beyond the Phase 2 PRD

## Gap Areas

### Gap 1: Capability-Pool Routing Policy Is Not Deterministic
Phase 2 requires:
- capability-pool work to match eligible runtimes by capability
- priority order or deterministic tie-breaking

Current state:
- the control plane can route to capability queues
- a runtime can claim capability-targeted work
- the claim path selects an eligible idle agent for a capability

Missing:
- explicit deterministic tie-breaking or routing policy
- persisted or configurable routing policy
- operator-visible explanation for why one candidate agent won over another

Why this matters:
- without deterministic routing, behavior depends on row order and incidental database state
- this violates the PRD's requirement that capability-pool routing be stable and extensible

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)

### Gap 2: Handoff Provenance Validation Is Too Weak
Phase 2 requires:
- explicit handoff metadata between jobs
- result references through artifact IDs
- preserved provenance for follow-on jobs

Current state:
- handoff records exist
- child jobs are created
- source artifact IDs are recorded
- lineage events are emitted

Missing:
- verification that supplied artifact IDs exist
- verification that supplied artifact IDs belong to the source job
- rejection of invalid or unrelated artifacts
- stronger validation of handoff fan-out inputs

Why this matters:
- current handoffs can record invalid provenance while still looking structurally correct
- this weakens artifact lineage and makes inspection less trustworthy

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [models.py](/home/user/projects/skynet/src/agp/models.py)

### Gap 3: Runtime Inspection Does Not Include Claimed Work
Phase 2 requires:
- `GET /runtimes` to return runtime health and claimed work

Current state:
- runtimes can be listed
- status and health status are visible
- runtime logs and traces are available elsewhere

Missing:
- claimed runs or active jobs on each runtime in the runtime list surface
- active lease summary per runtime
- operator-friendly per-runtime work visibility without joining other APIs manually

Why this matters:
- operators should be able to see not just that a runtime is `busy`, but what it is actually running
- this is a Phase 2 observability and triage requirement, not merely a convenience feature

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)

### Gap 4: Artifact Retrieval Does Not Yet Support Large-Output Semantics
Phase 2 requires:
- artifact APIs to support large output retrieval without requiring full inline payloads

Current state:
- artifact metadata can be fetched
- artifact content can be fetched
- shared and registry-backed artifact stores exist

Missing:
- pagination or range retrieval for large artifact content
- explicit retrieval handles or download-oriented behavior for large outputs
- clear distinction between small inline reads and large payload access

Why this matters:
- Phase 2 expects artifact retrieval to scale beyond small text payloads
- current direct `content` responses are sufficient for MVP inspection, not for the full PRD requirement

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [artifact_store.py](/home/user/projects/skynet/src/agp/artifact_store.py)

### Gap 5: Persisted Health History Is Missing
Phase 2 requires:
- health records with:
- `entity_type`
- `entity_id`
- `health_status`
- `reason`
- `observed_at`

Current state:
- runtime `status` and `health_status` exist
- stale-runtime transitions and alerts exist
- health is visible in current state and derived alerts

Missing:
- a dedicated persisted health-record model
- historical health observations over time
- queryable audit trail of health transitions independent of current runtime row state

Why this matters:
- current health visibility is state-based and event-based, but not modeled as the PRD specifies
- that limits historical analysis and clean separation between current state and health observations

Affected implementation:
- [models.py](/home/user/projects/skynet/src/agp/models.py)
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)

### Gap 6: Capability-Pool Metadata Is Missing As A Durable Model
Phase 2 requires:
- `capability_pools`
- `capability_id`
- `queue_id`
- `routing_policy`

Current state:
- capability-pool queues are derived in code from capability id and version

Missing:
- explicit persisted capability-pool rows
- explicit routing-policy field
- operator-visible management of capability-pool metadata

Why this matters:
- deriving queue ids in code works for simple operation
- it does not satisfy the Phase 2 PRD's requirement for a durable capability-pool model
- it also blocks future policy evolution from being data-driven

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [models.py](/home/user/projects/skynet/src/agp/models.py)

### Gap 7: Agent-Runtime Binding History Exists In Schema But Is Not Operationally Used
Phase 2 requires:
- durable agent identity independent of runtime churn
- data model support for agent-runtime bindings

Current state:
- `agent_runtime_bindings` exists in the schema
- agent reassignment and stale-runtime detachment behavior exist

Missing:
- actual writes to `agent_runtime_bindings`
- binding lifecycle history as part of runtime churn
- operator-facing inspection of binding changes over time

Why this matters:
- the platform behaves as if durable bindings matter
- but the binding history is not actually captured in the durable model

Affected implementation:
- [models.py](/home/user/projects/skynet/src/agp/models.py)
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)

### Gap 8: Runtime Degradation Semantics Are Only Partially Real
Phase 2 requires:
- runtime states including `degraded`
- health classification including `degraded`

Current state:
- runtime rows expose `status` and `health_status`
- `unreachable`, `draining`, and `offline` behavior exist
- agents can become degraded when their runtime goes offline mid-lease

Missing:
- a real runtime transition into `RuntimeStatus.DEGRADED`
- a real runtime transition into `HealthStatus.DEGRADED`
- routing policy behavior tied to degraded runtimes

Why this matters:
- the PRD distinguishes degraded from unreachable
- current implementation mostly jumps from healthy to unreachable/offline at the runtime layer
- this leaves a gap between the documented health model and the actual platform states

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)

### Gap 9: Operator Visibility For Active Jobs And Recent Failures Is Distributed Across Surfaces
Phase 2 requires:
- active jobs
- recent failures
- stale runtimes
- current capabilities
- artifact references
- event timelines

Current state:
- the data is mostly available across:
- `/jobs`
- `/jobs/{job_id}/events`
- `/runtimes`
- `/queue/deliveries`
- `/observability/*`
- artifact endpoints

Missing:
- one clearly Phase-2-shaped inspection surface for recent failures
- direct active-job summaries grouped by runtime
- a tighter operator triage path without cross-joining several endpoints manually

Why this matters:
- Phase 2 wants the platform to be inspectable, not merely queryable
- the current implementation is powerful but somewhat fragmented for operator workflows

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [cli.py](/home/user/projects/skynet/src/agp/cli.py)

### Gap 10: Phase 2 Test Strategy Is Only Partially Locked Down
Phase 2 requires:
- multi-runtime integration tests
- network partition and reconnect tests
- duplicate claim prevention tests
- lease expiry and reassignment tests
- handoff and artifact-chaining tests
- operator inspection API tests
- fencing-token rejection tests
- agent-runtime replacement tests

Current state:
- many of these areas already have direct coverage
- failure-injection drills exist
- handoff, lease expiry, repeated fencing, stale-runtime behavior, and some replacement paths are tested

Missing:
- explicit network partition and reconnect tests
- explicit duplicate-claim prevention tests as a named invariant
- stronger test coverage for runtime inspection depth
- explicit tests for the Phase 2 data-model gaps above

Why this matters:
- several behaviors are currently proven indirectly rather than directly
- that is enough for implementation confidence in some cases, but not enough for clean Phase 2 exit evidence

Affected implementation:
- [tests/test_mvp_flow.py](/home/user/projects/skynet/tests/test_mvp_flow.py)

## Priority

### Priority 0
- deterministic capability-pool routing
- strict handoff artifact validation
- runtime claimed-work summaries

### Priority 1
- persisted health-record model
- operational use of agent-runtime binding history
- capability-pool durable metadata

### Priority 2
- large-artifact retrieval semantics
- tighter operator triage surfaces
- direct test coverage for remaining Phase 2 guarantees

## Required Deliverables

### Deliverable 1: Deterministic Routing
The platform must define and implement a deterministic candidate-selection policy for capability-pool work.

Minimum expectation:
- explicit ordering rule
- stable behavior across repeated claims
- test coverage proving the rule

### Deliverable 2: Validated Handoff Provenance
The platform must reject handoffs that reference invalid or unrelated artifacts.

Minimum expectation:
- source artifacts must exist
- source artifacts must belong to the source job
- invalid handoffs must fail cleanly

### Deliverable 3: Runtime Claimed-Work Inspection
Operators must be able to inspect what each runtime is currently running.

Minimum expectation:
- active run count or active run summaries on runtime inspection surfaces
- active lease visibility tied to runtime rows or runtime detail surfaces

### Deliverable 4: Missing Durable Metadata Models
The platform must either:
- implement the missing Phase 2 durable models, or
- explicitly revise the Phase 2 PRD if those models are intentionally collapsed into other structures

This applies to:
- health records
- capability pools
- operational use of agent-runtime bindings

### Deliverable 5: Large Artifact Retrieval Contract
Artifact retrieval must support large-output semantics without assuming small inline payloads.

Minimum expectation:
- pagination, range retrieval, or explicit download-handle behavior

### Deliverable 6: Phase 2 Exit Test Coverage
The remaining unproven Phase 2 requirements must be covered directly by tests.

## Acceptance Criteria
Phase 2 may be considered complete when all of the following are true:

- capability-pool routing uses a deterministic, test-covered selection rule
- handoff requests reject invalid or unrelated artifact references
- runtime inspection exposes claimed work clearly enough for operator triage
- the data-model gaps are either implemented or intentionally removed from the authoritative Phase 2 PRD
- artifact retrieval supports a large-output access path beyond simple inline reads
- the remaining Phase 2 distributed guarantees have direct regression coverage

## Exit Criteria
This gap document is complete when it is no longer needed because:
- the missing behaviors have been implemented and tested
- the authoritative Phase 2 PRD and the implementation match closely enough that no material delivery-gap document remains necessary
