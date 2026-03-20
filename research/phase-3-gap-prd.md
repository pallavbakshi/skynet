# Product Requirements Document

## Document
AGP Phase 3 Gap PRD

## Version
0.1 Draft

## Purpose
Define the gap between AGP's current implementation and the target defined in the Phase 3 Technical PRD.

This document is not a replacement for the Phase 3 Technical PRD. It is a delivery-gap document that answers:
- what Phase 3 capabilities are already present in the codebase
- what Phase 3 capabilities are only partially implemented
- what is still missing before AGP can be considered complete for Phase 3

## Scope
This document evaluates the current codebase against:
- [Phase 3 Technical PRD](/home/user/projects/skynet/research/phase-3-technical-prd.md)

It focuses on:
- deployed infrastructure shape
- shared persistence and service dependencies
- secrets and service identity
- observability and operational surfaces
- backup, restore, upgrade, and recovery
- deployment validation and failure-drill evidence

It does not restate Phase 1 or Phase 2 product semantics except where those semantics must now be proven on real infrastructure.

## Summary
Phase 3 is partially implemented.

AGP already has several serious operational building blocks:
- `compose.yaml` for a multi-service local stack
- `k8s/` manifests for deployment parity with the current stack
- Redis queue backend support
- registry-backed and shared-filesystem-backed artifact storage modes
- operator observability surfaces for summaries, traces, alerts, and logs
- local backup, restore, validation, and queue reconstruction workflows
- upgrade metadata, skew checks, and rollback-target tracking
- operator RBAC and token rotation surfaces

However, Phase 3 is not complete yet.

The remaining gaps are not in the core control-loop semantics. They are concentrated in:
- real production-style shared state and HA topology
- validated deployment and recovery on real infrastructure
- stronger service identity and transport security
- full observability baseline with metrics, dashboards, and notification hooks
- production-grade DR, rollout, and operational runbooks
- executable evidence that the system meets the Phase 3 acceptance criteria

## Current State

### Implemented Phase 3 Areas
- local multi-service deployment assets:
- [compose.yaml](/home/user/projects/skynet/compose.yaml)
- [k8s/](/home/user/projects/skynet/k8s)
- shared artifact backend options:
- [artifact_store.py](/home/user/projects/skynet/src/agp/artifact_store.py)
- Redis-backed queue transport:
- [queue_backend.py](/home/user/projects/skynet/src/agp/queue_backend.py)
- operator observability read surfaces:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- local backup, restore, validation, and recovery commands:
- [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- upgrade status, skew enforcement, rollback-target metadata, and token rotation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [runtime.py](/home/user/projects/skynet/src/agp/runtime.py)

### Phase 3 Areas That Exist But Are Weaker Than The PRD
- deployment automation exists, but is not fully validated on live infrastructure
- Kubernetes manifests exist, but are still single-node/single-control-plane oriented
- secrets handling exists, but is still application-token-centric rather than infrastructure-identity-centric
- observability exists, but lacks a true metrics backend, dashboard layer, and alert delivery path
- backup and restore exists, but is still local-first rather than infrastructure-grade
- rollout and rollback logic exists, but full deployed-service upgrade choreography is not implemented

## Problem Statement
The implementation now has much of the operational scaffolding that Phase 3 requires, but it does not yet prove that AGP runs as a complete production-style hosted system.

The largest remaining weaknesses are:
- infrastructure assumptions are still local-first
- shared persistence and HA are not yet real in the deployed model
- observability is application-surface-heavy but infrastructure-stack-light
- security is authenticated, but not yet strongly anchored in transport identity, secret delivery, and service boundary enforcement
- deployment and disaster-recovery procedures are only partially validated as executable operational practice

Because of this, AGP cannot yet honestly claim full conformance to the Phase 3 PRD.

## Goal
Close the remaining gaps so AGP can be considered a coherent Phase 3 hosted platform with:
- validated deployment on real multi-service infrastructure
- real shared persistence and durable backend topology
- explicit and enforceable service identity and security boundaries
- production-grade observability and operator response surfaces
- tested backup, restore, rollout, and recovery workflows
- evidence that the Phase 3 acceptance and exit criteria are satisfied

## Non-Goals
- global multi-region failover
- tenant-isolated platform architecture
- replacing the Phase 1 / Phase 2 control-loop model
- redesigning the plugin-based runtime architecture
- introducing advanced policy engines beyond the current platform scope

## Gap Areas

### Gap 1: Shared Networked State Store Is Not Yet Productionized
Phase 3 requires:
- durable relational storage for system state
- shared networked state across deployed services
- highly available `state-store`

Current state:
- AGP still defaults to SQLite-style local persistence for much of its tested path
- deployment assets mount shared volumes around a single control-plane instance
- the data model and migration exist, but the actual deployed state-store architecture is not yet production-grade

Missing:
- a real shared networked relational database deployment model
- deployment assets and configuration for that database
- failover and restart validation for the state store
- evidence that control-plane state survives real backend restart under deployed conditions

Why this matters:
- Phase 3 requires infrastructure-backed truth, not just local correctness
- a local DB path is acceptable for development, not for claiming hosted, HA-capable platform behavior

Affected implementation:
- [db.py](/home/user/projects/skynet/src/agp/db.py)
- [migrations/0001_initial.sql](/home/user/projects/skynet/migrations/0001_initial.sql)
- [compose.yaml](/home/user/projects/skynet/compose.yaml)
- [k8s/](/home/user/projects/skynet/k8s)

### Gap 2: HA Topology For Core Services Is Not Yet Real
Phase 3 requires:
- `control-plane`, `state-store`, and `queue` to be highly available
- explicit HA topology documentation and recovery expectations

Current state:
- `compose.yaml` and `k8s/` exist
- they express a useful deployment footprint
- but they are single-control-plane and development-oriented
- Redis is present, but its HA/managed deployment assumptions are not implemented

Missing:
- replicated or otherwise HA-capable control-plane strategy
- real HA queue strategy
- state-store redundancy plan in the deployed topology
- validated service restart and failover drills at the infrastructure level

Why this matters:
- Phase 3 acceptance requires restartability and recovery without semantic change
- topology parity is not enough if the hosted shape is still single-instance fragile

Affected implementation:
- [compose.yaml](/home/user/projects/skynet/compose.yaml)
- [k8s/control-plane.yaml](/home/user/projects/skynet/k8s/control-plane.yaml)
- [k8s/redis.yaml](/home/user/projects/skynet/k8s/redis.yaml)
- [deployment-architecture-spec.md](/home/user/projects/skynet/research/deployment-architecture-spec.md)

### Gap 3: Deployment Assets Exist, But Live Deployment Validation Is Still Missing
Phase 3 requires:
- AGP to be deployable as a complete system across multiple computers
- all required services to be operational together
- deployment automation and runbooks to be executable

Current state:
- local deployment assets exist
- bootstrap and smoke scripts exist
- Kubernetes manifests exist
- earlier implementation work explicitly noted that Docker and `kubectl` validation could not be run in this environment

Missing:
- checked-in validation evidence for real `docker compose` bring-up
- checked-in validation evidence for `kubectl` or equivalent cluster deployment
- multi-computer deployment test harness or runbook-backed proof
- automated deployment smoke path as part of verification

Why this matters:
- deployment files alone do not satisfy the Phase 3 acceptance criteria
- Phase 3 is about operational proof, not just manifest presence

Affected implementation:
- [scripts/bootstrap_local_stack.py](/home/user/projects/skynet/scripts/bootstrap_local_stack.py)
- [scripts/smoke_local_stack.py](/home/user/projects/skynet/scripts/smoke_local_stack.py)
- [compose.yaml](/home/user/projects/skynet/compose.yaml)
- [k8s/README.md](/home/user/projects/skynet/k8s/README.md)

### Gap 4: Secrets And Service Identity Are Still Too Application-Centric
Phase 3 requires:
- secrets injection
- service identity
- explicit authentication and authorization boundaries
- rotatable service credentials and certificates

Current state:
- operator/runtime bearer-token auth exists
- RBAC exists
- token rotation exists and is persisted
- Kubernetes manifests use `Secret` resources for environment injection

Missing:
- service identity stronger than bearer tokens
- infrastructure-native secret delivery strategy
- explicit credential classes for control plane, runtimes, queue, state store, and artifact store
- certificate lifecycle and rotation plan
- auditable operator access model tied to deployment identity rather than only app-level tokens

Why this matters:
- Phase 3 security is not only about API auth
- infrastructure-backed hosting requires explicit identity and secret boundaries for services and dependencies

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [config.py](/home/user/projects/skynet/src/agp/config.py)
- [security-model-spec.md](/home/user/projects/skynet/research/security-model-spec.md)
- [k8s/secret.yaml](/home/user/projects/skynet/k8s/secret.yaml)

### Gap 5: Encryption-In-Transit And Store Access Boundaries Are Not Enforced End To End
Phase 3 requires:
- secure service-to-service communication
- encryption in transit
- access control boundaries for artifact and state backends

Current state:
- runtime-to-control-plane communication is authenticated
- deployment assets define service wiring
- security docs explicitly treat mTLS as later-phase relative to the V1 implementation

Missing:
- enforced TLS or mTLS in the deployed topology
- backend credential isolation for state store, queue, and artifact store
- store-level access policies proving least-privilege boundaries
- validation of secure service-to-service transport in deployed environments

Why this matters:
- Phase 3 moves AGP from local development posture to hosted operational posture
- app-level auth without transport and backend boundary enforcement is not enough

Affected implementation:
- [control-plane-api-spec.md](/home/user/projects/skynet/research/control-plane-api-spec.md)
- [security-model-spec.md](/home/user/projects/skynet/research/security-model-spec.md)
- [compose.yaml](/home/user/projects/skynet/compose.yaml)
- [k8s/](/home/user/projects/skynet/k8s)

### Gap 6: Shared Artifact Durability Is Improved, But Not Yet Fully Production-Grade
Phase 3 requires:
- durable shared artifact storage
- artifact immutability or equivalent write-once semantics
- operational redundancy

Current state:
- multiple artifact backends exist:
- `localfs`
- `sharedfs`
- `registryfs`
- `inmemory`
- artifact checksum support exists
- artifact validation is enforced before terminal state

Missing:
- a clearly production-oriented object-store or managed durable backend
- stronger operational redundancy assumptions for the deployed artifact store
- store-level access-boundary enforcement
- end-to-end validation of artifact durability under deployed restart/failure scenarios

Why this matters:
- AGP has real artifact abstractions now
- but Phase 3 requires durable hosted storage, not only interchangeable local/shared filesystem modes

Affected implementation:
- [artifact_store.py](/home/user/projects/skynet/src/agp/artifact_store.py)
- [artifact-and-finalization-spec.md](/home/user/projects/skynet/research/artifact-and-finalization-spec.md)

### Gap 7: Observability Exists, But The Full Production Baseline Is Not Yet Deployed
Phase 3 requires:
- metrics
- traces
- logs
- alerts
- runtime fleet visibility
- monitoring dashboards

Current state:
- AGP exposes:
- observability summaries
- active alerts
- per-job traces
- structured control-plane logs
- structured runtime logs
- log rotation and pruning

Missing:
- a real metrics export path and metrics backend
- dashboard definitions and deployed dashboard surfaces
- alert notification hooks or integrations
- explicit infrastructure health telemetry for queue, state store, and artifact store
- distributed tracing infrastructure beyond app-derived trace responses

Why this matters:
- AGP currently has strong operator read APIs
- Phase 3 requires actual deployed operational visibility, not only application-level inspection endpoints

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [runtime.py](/home/user/projects/skynet/src/agp/runtime.py)
- [logs.py](/home/user/projects/skynet/src/agp/logs.py)
- [observability-spec.md](/home/user/projects/skynet/research/observability-spec.md)

### Gap 8: Backup, Restore, And DR Are Still Local-First Rather Than Infrastructure-Grade
Phase 3 requires:
- backup and restore for state and artifacts
- preservation of state-to-artifact references
- executable restore procedures
- RPO/RTO-backed recovery expectations

Current state:
- AGP has:
- backup snapshot creation
- restore
- validation
- queue reconstruction from authoritative state
- combined restore-and-recover path

Missing:
- production-style backup integration for shared networked state store
- production-style backup integration for a durable shared artifact backend
- queue/state/artifact recovery procedures validated against deployed services
- RPO/RTO validation evidence for the documented targets

Why this matters:
- current DR workflows are serious and useful
- but they are scoped to local or development-style hosting assumptions
- Phase 3 requires real operational recoverability, not only local snapshot correctness

Affected implementation:
- [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- [backup-restore-and-dr-spec.md](/home/user/projects/skynet/research/backup-restore-and-dr-spec.md)

### Gap 9: Upgrade And Rollout Semantics Exist, But Deployed Service Choreography Is Incomplete
Phase 3 requires:
- rolling upgrade support for stateless services
- explicit schema migration strategy
- version-skew tolerance
- rollback procedures that preserve correctness

Current state:
- AGP has:
- runtime registration skew checks
- persisted upgrade status
- previous release/schema tracking
- rollback target metadata
- CLI rollback support

Missing:
- deployment-level rollout automation for services
- migration orchestration under real deployed dependencies
- explicit runtime-fleet rollout strategy
- validated rollback drill in deployed environments

Why this matters:
- version metadata alone does not prove safe production rollout
- Phase 3 requires that upgrade and rollback work operationally, not just logically

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- [upgrade-and-rollback-spec.md](/home/user/projects/skynet/research/upgrade-and-rollback-spec.md)

### Gap 10: Runbooks And Operational Deliverables Are Not Yet Complete As Executable Assets
Phase 3 requires:
- backup and restore procedures
- health check endpoints and alert conditions
- runbooks for runtime failure, control-plane failure, and storage incidents
- HA topology documentation
- recovery procedure documentation

Current state:
- specs exist for:
- deployment
- security
- observability
- backup/restore/DR
- upgrade/rollback
- failure injection
- some operator commands and scripts exist

Missing:
- operator-facing runbook assets that are both current and executable
- incident-specific procedures tied directly to the deployment assets
- packaged recovery workflows for queue outage, state-store outage, artifact-store outage, and control-plane outage

Why this matters:
- Phase 3 requires operational supportability, not just code and prose
- the repo still leans more heavily on specs than on complete runbook automation

Affected implementation:
- [deployment-architecture-spec.md](/home/user/projects/skynet/research/deployment-architecture-spec.md)
- [backup-restore-and-dr-spec.md](/home/user/projects/skynet/research/backup-restore-and-dr-spec.md)
- [failure-injection-test-plan.md](/home/user/projects/skynet/research/failure-injection-test-plan.md)

### Gap 11: Phase 3 Failure Drills And Acceptance Evidence Are Still Partial
Phase 3 requires:
- full-environment deployment tests
- service restart and recovery tests
- state backup and restore validation
- artifact durability validation
- queue restart and redelivery validation
- runtime replacement and rollout tests
- observability and alert smoke tests
- failure-domain drills for control plane, queue, and state store
- certificate and credential rotation tests

Current state:
- AGP has a strong suite of platform-level drills and regression tests
- some operational failure cases are covered
- credential rotation is implemented at the application-token level

Missing:
- full-environment deployment test evidence
- infrastructure-level control-plane failure drills
- infrastructure-level queue and state-store outage drills
- certificate rotation tests
- documented proof that the RPO/RTO targets are met
- direct evidence for the full Phase 3 acceptance criteria

Why this matters:
- the codebase has strong correctness evidence
- Phase 3 requires infrastructure-grade evidence, which is a different threshold

Affected implementation:
- [test_mvp_flow.py](/home/user/projects/skynet/tests/test_mvp_flow.py)
- [failure-injection-test-plan.md](/home/user/projects/skynet/research/failure-injection-test-plan.md)

## Priority

### Priority 0
- shared networked state-store deployment model
- HA topology for core services
- live deployment validation on real infrastructure
- full-environment failure and recovery evidence

### Priority 1
- stronger secrets and service identity model
- transport encryption and backend access-boundary enforcement
- production-grade observability stack
- infrastructure-grade backup, restore, and rollout workflows

### Priority 2
- object-store or managed artifact backend
- richer executable runbooks and incident automation
- deeper integration of certificate/service-identity rotation into deployment operations

## Required Deliverables
- shared state-store deployment design and implementation
- HA topology definition and deployment assets for core services
- validated deployment smoke path for `compose` and cluster deployment
- service identity and secrets-management implementation plan
- transport-security enforcement plan and assets
- metrics export, dashboard definitions, and alert notification integration
- infrastructure-grade backup, restore, and recovery workflows
- deployed upgrade and rollback runbooks and validation
- Phase 3 failure-drill suite and evidence package

## Acceptance Criteria
- AGP deploys as a complete system across multiple computers using real shared dependencies.
- `control-plane`, `runtimes`, `state-store`, `queue`, and `artifact-store` are operational together in a validated deployment.
- Shared state survives service restart without semantic corruption.
- Core service restart, queue redelivery, and runtime replacement preserve platform truth.
- Observability includes metrics, traces, logs, and actionable alerts in the deployed environment.
- Backup and restore preserve state-to-artifact correctness under the chosen hosted architecture.
- Upgrade and rollback workflows are validated against the deployed service topology.
- Security boundaries for services, operators, and backend dependencies are explicit, enforced, and auditable.

## Exit Criteria
Phase 3 is complete when AGP is not merely deployable in theory, but is proven to run as a hosted multi-service platform with:
- shared durable infrastructure
- validated deployment automation
- enforceable security boundaries
- operator-grade observability
- tested recovery workflows
- evidence that the documented RPO/RTO and Phase 3 acceptance expectations are met
