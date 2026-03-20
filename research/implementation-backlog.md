# AGP Implementation Backlog

## Purpose
This document turns the authoritative AGP specs into an implementation-oriented backlog.

## Governing Documents
Core authoritative specs:
- [Job / Run / Lease Protocol](/home/user/projects/skynet/job-run-lease-protocol.md)
- [State Machine Spec](/home/user/projects/skynet/state-machine-spec.md)
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [Data Model Spec](/home/user/projects/skynet/data-model-spec.md)
- [Artifact and Finalization Spec](/home/user/projects/skynet/artifact-and-finalization-spec.md)
- [Agent Lifecycle Spec](/home/user/projects/skynet/agent-lifecycle-spec.md)
- [Queue Topology and Routing Spec](/home/user/projects/skynet/queue-topology-and-routing-spec.md)
- [Runtime Supervision Spec](/home/user/projects/skynet/runtime-supervision-spec.md)
- [Event Model Spec](/home/user/projects/skynet/event-model-spec.md)
- [Orchestration Surface Spec](/home/user/projects/skynet/orchestration-surface-spec.md)
- [Handoff and Provenance Spec](/home/user/projects/skynet/handoff-and-provenance-spec.md)
- [Capability Registry Spec](/home/user/projects/skynet/capability-registry-spec.md)

Operational specs:
- [Deployment Architecture Spec](/home/user/projects/skynet/deployment-architecture-spec.md)
- [Security Model Spec](/home/user/projects/skynet/security-model-spec.md)
- [Observability Spec](/home/user/projects/skynet/observability-spec.md)
- [Backup, Restore, and DR Spec](/home/user/projects/skynet/backup-restore-and-dr-spec.md)
- [Upgrade and Rollback Spec](/home/user/projects/skynet/upgrade-and-rollback-spec.md)
- [Failure Injection Test Plan](/home/user/projects/skynet/failure-injection-test-plan.md)

## Priority 0: Foundations

### 1. Finalize API schema from stub
Depends on:
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [OpenAPI Stub](/home/user/projects/skynet/openapi.yaml)

Tasks:
- expand all request/response schemas
- define pagination envelopes
- define standard error payloads
- define auth headers and runtime identity hooks

### 2. Finalize relational schema
Depends on:
- [Data Model Spec](/home/user/projects/skynet/data-model-spec.md)
- [Initial DB Migration Stub](/home/user/projects/skynet/migrations/0001_initial.sql)

Tasks:
- add enum/check constraints
- enforce active-lease uniqueness
- add event sequence generation
- define cascade / delete behavior
- review FK deferrability

### 3. Implement state transition guards
Depends on:
- [State Machine Spec](/home/user/projects/skynet/state-machine-spec.md)

Tasks:
- encode legal transitions
- reject illegal transitions
- centralize terminal-state checks
- add invariant checks for single active run / lease

## Priority 1: Core Control Plane

### 4. Build message send path
Depends on:
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [State Machine Spec](/home/user/projects/skynet/state-machine-spec.md)
- [Orchestration Surface Spec](/home/user/projects/skynet/orchestration-surface-spec.md)

Tasks:
- accept `send`
- create message + job
- choose sync vs async response path
- enqueue to agent or capability queue
- emit events

### 5. Build claim / heartbeat / completion path
Depends on:
- [Job / Run / Lease Protocol](/home/user/projects/skynet/job-run-lease-protocol.md)
- [Queue Topology and Routing Spec](/home/user/projects/skynet/queue-topology-and-routing-spec.md)

Tasks:
- implement pull-based claim
- issue leases and fencing tokens
- implement heartbeat renewal
- implement success/failure acceptance
- reject stale fencing tokens

### 6. Implement interrupt and cancellation path
Depends on:
- [Job / Run / Lease Protocol](/home/user/projects/skynet/job-run-lease-protocol.md)
- [State Machine Spec](/home/user/projects/skynet/state-machine-spec.md)

Tasks:
- queued job cancellation
- running job interruption
- terminal cancellation events
- forced teardown handling

## Priority 2: Runtime and Agent Management

### 7. Build runtime registration and liveness
Depends on:
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [Data Model Spec](/home/user/projects/skynet/data-model-spec.md)
- [Runtime Supervision Spec](/home/user/projects/skynet/runtime-supervision-spec.md)

Tasks:
- runtime register/refresh endpoint
- runtime state + health updates
- stale/offline detection hooks

### 8. Build durable agent lifecycle
Depends on:
- [Agent Lifecycle Spec](/home/user/projects/skynet/agent-lifecycle-spec.md)

Tasks:
- `agents/up`
- `agents/down`
- agent queue creation
- drain and force-down logic
- idle-timeout policy hooks

### 9. Build runtime local supervision contract
Depends on:
- [Job / Run / Lease Protocol](/home/user/projects/skynet/job-run-lease-protocol.md)
- [Agent Lifecycle Spec](/home/user/projects/skynet/agent-lifecycle-spec.md)
- [Runtime Supervision Spec](/home/user/projects/skynet/runtime-supervision-spec.md)

Tasks:
- CLI execution wrapper
- heartbeat scheduler
- local recovery loop
- fencing callback behavior

## Priority 3: Artifacts and Finalization

### 10. Implement artifact metadata + storage abstraction
Depends on:
- [Artifact and Finalization Spec](/home/user/projects/skynet/artifact-and-finalization-spec.md)
- [Data Model Spec](/home/user/projects/skynet/data-model-spec.md)

Tasks:
- artifact create/store path
- metadata persistence
- content retrieval endpoint
- artifact role enforcement

### 11. Implement write-first finalization
Depends on:
- [Artifact and Finalization Spec](/home/user/projects/skynet/artifact-and-finalization-spec.md)

Tasks:
- artifact-first success path
- artifact-first failure path
- state finalization after artifact durability
- orphan detection hooks

## Priority 4: Read Surfaces

### 12. Build jobs, events, artifacts, agents, runtimes read APIs
Depends on:
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [Event Model Spec](/home/user/projects/skynet/event-model-spec.md)

Tasks:
- list/filter endpoints
- cursor pagination
- ordered event feeds
- artifact metadata/content views

### 13. Build orchestration CLI
Depends on:
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [Orchestration Surface Spec](/home/user/projects/skynet/orchestration-surface-spec.md)

Tasks:
- `send`
- `watch`
- `interrupt`
- `fetch`
- `agents`
- `jobs`

## Priority 5: Reliability and Multi-Phase Expansion

### 14. Implement retry and reassignment policy
Depends on:
- [Job / Run / Lease Protocol](/home/user/projects/skynet/job-run-lease-protocol.md)

Tasks:
- lease-expiry retry
- retry budget enforcement
- new-run creation for retries
- reassignment eligibility checks

### 15. Implement handoff flow
Depends on:
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [Data Model Spec](/home/user/projects/skynet/data-model-spec.md)
- [Handoff and Provenance Spec](/home/user/projects/skynet/handoff-and-provenance-spec.md)

Tasks:
- handoff record creation
- source artifact validation
- child-job creation
- provenance preservation

## Priority 6: Verification

### 16. Build invariant and protocol test suite
Depends on all core specs

Tasks:
- state transition tests
- duplicate terminal report tests
- stale fencing token tests
- queue redelivery tests
- artifact finalization consistency tests
- agent teardown tests

### 17. Build integration drills for later phases
Depends on Phase 2 / Phase 3 work

Tasks:
- multi-runtime claim tests
- lease-expiry reassignment tests
- failure-domain drills
- backup/restore validation
- observability smoke tests
- authn/authz validation

## Suggested Execution Order
1. schema + migration hardening
2. state transition engine
3. send + claim + lease + heartbeat
4. artifact storage + finalization
5. agent lifecycle
6. read APIs + CLI
7. retry / reassignment
8. handoff
9. distributed and infrastructure expansion
