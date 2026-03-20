# Product Requirements Document

## Document
AGP Phase 3 Gap PRD

## Version
0.2

## Purpose
Define the remaining gap between AGP's current implementation and the target defined in the Phase 3 Technical PRD.

This is a delivery-gap document. It answers:
- what Phase 3 capabilities are already present in the repo
- which Phase 3 areas are now only partial gaps
- what is still missing before AGP can honestly claim Phase 3 completion

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

It does not restate Phase 1 or Phase 2 control-loop semantics except where those semantics must now be proven on real infrastructure.

## Summary
Phase 3 is still incomplete, but the gap is materially smaller than the original draft of this document implied.

AGP already has real Phase 3 scaffolding:
- networked queue transport with Redis support
- networked-state deployment assets for PostgreSQL in:
  - [compose.phase3.yaml](/home/user/projects/skynet/compose.phase3.yaml)
  - [k8s/postgres.yaml](/home/user/projects/skynet/k8s/postgres.yaml)
- registry-backed and shared-filesystem-backed artifact modes in [artifact_store.py](/home/user/projects/skynet/src/agp/artifact_store.py)
- operator observability APIs for:
  - summaries
  - alerts
  - metrics export
  - traces
  - control-plane logs
  - runtime logs
  - triage
  - health-record history
- alert webhook dispatch support in [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- local backup, restore, validation, and queue reconstruction workflows in [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- upgrade status, skew checks, rollback-target tracking, RBAC, and token rotation

The remaining Phase 3 gaps are now concentrated in infrastructure proof and production hardening:
- HA topology is still not real
- live deployment validation on actual Docker/Kubernetes infrastructure is still missing
- service identity and transport security are still token-centric
- observability still lacks deployed dashboards and external metrics/alert backends
- DR and rollout procedures are still stronger in local/dev form than in production form
- the repo still lacks full failure-drill evidence for the hosted environment

## Current State

### Areas Effectively Closed Or Mostly Closed
- queue transport abstraction and external broker path:
  - [queue_backend.py](/home/user/projects/skynet/src/agp/queue_backend.py)
- artifact abstraction, checksums, and terminal-state artifact validation:
  - [artifact_store.py](/home/user/projects/skynet/src/agp/artifact_store.py)
  - [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- operator observability read surfaces:
  - [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- Prometheus-style metrics export and alert webhook dispatch:
  - [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- local backup, restore, validation, and recovery commands:
  - [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- upgrade state, rollback-target metadata, auth status, and token rotation:
  - [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
  - [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- executable deployment-asset validation helper:
  - [scripts/validate_phase3_assets.py](/home/user/projects/skynet/scripts/validate_phase3_assets.py)

### Areas That Exist But Are Still Weaker Than The Phase 3 PRD
- PostgreSQL-backed deployment assets now exist, but HA database topology is still absent
- Kubernetes assets now use a networked state store, but they are still single-control-plane and single-Postgres
- observability now includes metrics export and webhook dispatch, but not a deployed metrics backend or dashboard layer
- security now includes RBAC and rotating tokens, but not transport identity or mTLS
- backup and restore are real, but still not infrastructure-grade for a managed shared backend
- rollout metadata and skew checks exist, but service-fleet choreography is still incomplete

## Problem Statement
AGP now has much of the infrastructure scaffolding Phase 3 requires, but it still does not prove that AGP runs as a complete hosted multi-service platform with HA-grade properties.

The biggest remaining weaknesses are:
- topology is still single-instance for key services
- deployment assets are stronger than before, but still not validated on live infrastructure in this environment
- security boundaries are enforced at the application layer more than the transport and backend layers
- observability is export-capable, but not yet backed by deployed monitoring systems
- disaster recovery, rollout, and failure evidence remain local-first or operator-manual

Because of this, AGP still cannot honestly claim full conformance to the Phase 3 PRD.

## Goal
Close the remaining gaps so AGP can be considered a coherent Phase 3 hosted platform with:
- validated deployment on real multi-service infrastructure
- real shared persistence and durable backend topology
- explicit and enforceable service identity and transport security boundaries
- production-grade observability with external monitoring integration
- tested backup, restore, rollout, and recovery workflows
- evidence that the Phase 3 acceptance and exit criteria are satisfied

## Non-Goals
- global multi-region failover
- tenant-isolated platform architecture
- replacing the Phase 1 / Phase 2 platform model
- redesigning the plugin-based runtime architecture
- inventing a custom metrics/alert backend inside AGP instead of integrating with standard infrastructure

## Gap Areas

### Gap 1: HA Topology For Core Services Is Still Not Real
Phase 3 requires:
- `control-plane`, `state-store`, and `queue` to be highly available
- explicit HA topology documentation and recovery expectations

Current state:
- networked PostgreSQL deployment assets now exist in:
  - [compose.phase3.yaml](/home/user/projects/skynet/compose.phase3.yaml)
  - [k8s/postgres.yaml](/home/user/projects/skynet/k8s/postgres.yaml)
- Redis is present
- Kubernetes and Compose topologies are still single-instance
- [k8s/README.md](/home/user/projects/skynet/k8s/README.md) still explicitly treats the stack as non-HA

Missing:
- replicated or leader-aware control-plane strategy
- HA Postgres strategy
- HA Redis or managed queue strategy
- failover drills that prove semantic correctness across service replacement

Why this matters:
- moving from SQLite to PostgreSQL removes one major Phase 3 blocker
- it does not by itself satisfy hosted HA behavior

Affected implementation:
- [compose.phase3.yaml](/home/user/projects/skynet/compose.phase3.yaml)
- [k8s/control-plane.yaml](/home/user/projects/skynet/k8s/control-plane.yaml)
- [k8s/postgres.yaml](/home/user/projects/skynet/k8s/postgres.yaml)
- [k8s/redis.yaml](/home/user/projects/skynet/k8s/redis.yaml)

### Gap 2: Live Deployment Validation Is Still Missing
Phase 3 requires:
- AGP to be deployable as a complete system across multiple computers
- all required services to be operational together
- deployment automation and runbooks to be executable

Current state:
- deployment assets exist for local and Phase 3 stack shapes
- [scripts/bootstrap_local_stack.py](/home/user/projects/skynet/scripts/bootstrap_local_stack.py) and [scripts/smoke_local_stack.py](/home/user/projects/skynet/scripts/smoke_local_stack.py) exist
- [scripts/validate_phase3_assets.py](/home/user/projects/skynet/scripts/validate_phase3_assets.py) now validates Compose/Kustomize assets when the host has the needed tools
- this environment still does not have `docker` or `kubectl`, so no checked-in live validation evidence was produced here

Missing:
- real `docker compose -f compose.phase3.yaml up` evidence
- real `kubectl apply -k k8s` evidence
- multi-computer deployment test evidence
- automated deployment smoke as part of CI or an ops verification workflow

Why this matters:
- manifests and scripts are necessary
- Phase 3 requires proof that they work together on real infrastructure

Affected implementation:
- [compose.phase3.yaml](/home/user/projects/skynet/compose.phase3.yaml)
- [k8s/](/home/user/projects/skynet/k8s)
- [scripts/validate_phase3_assets.py](/home/user/projects/skynet/scripts/validate_phase3_assets.py)
- [scripts/smoke_local_stack.py](/home/user/projects/skynet/scripts/smoke_local_stack.py)

### Gap 3: Secrets And Service Identity Are Still Application-Centric
Phase 3 requires:
- secrets injection
- service identity
- explicit authentication and authorization boundaries
- rotatable service credentials and certificates

Current state:
- operator/runtime bearer-token auth exists
- RBAC exists
- token rotation exists and is persisted
- Kubernetes manifests now inject a database URL and alert webhook value through `Secret`

Missing:
- service identity stronger than bearer tokens
- certificate lifecycle and rotation
- workload identity or equivalent infrastructure identity
- per-backend least-privilege credential separation beyond env-injected URLs/tokens

Why this matters:
- app-level tokens are useful
- Phase 3 hosted security expects stronger service identity and secret handling than that

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [config.py](/home/user/projects/skynet/src/agp/config.py)
- [k8s/secret.yaml](/home/user/projects/skynet/k8s/secret.yaml)
- [research/security-model-spec.md](/home/user/projects/skynet/research/security-model-spec.md)

### Gap 4: Encryption-In-Transit And Store Access Boundaries Are Not Enforced End To End
Phase 3 requires:
- secure service-to-service communication
- encryption in transit
- access-control boundaries for artifact and state backends

Current state:
- runtime-to-control-plane communication is authenticated
- service dependencies are now more explicit in the deployment assets

Missing:
- enforced TLS or mTLS for service-to-service traffic
- backend credential isolation for Postgres, Redis, and artifact storage
- explicit store-level access controls
- validation that secure transport is enabled in deployed environments

Why this matters:
- application auth without transport enforcement is not enough for hosted posture

Affected implementation:
- [compose.phase3.yaml](/home/user/projects/skynet/compose.phase3.yaml)
- [k8s/](/home/user/projects/skynet/k8s)
- [research/security-model-spec.md](/home/user/projects/skynet/research/security-model-spec.md)

### Gap 5: Observability Is Export-Capable, But Not Yet A Deployed Monitoring Stack
Phase 3 requires:
- metrics
- traces
- logs
- alerts
- runtime fleet visibility
- monitoring dashboards

Current state:
- AGP now exposes:
  - summaries
  - active alerts
  - per-job traces
  - structured control-plane logs
  - structured runtime logs
  - triage
  - health-record history
  - Prometheus-style metrics export
  - alert webhook dispatch

Missing:
- a deployed metrics backend such as Prometheus-compatible scrape configuration
- dashboard definitions
- alertmanager-style delivery integration beyond a simple webhook target
- infrastructure telemetry for Postgres/Redis/artifact backend
- distributed tracing infrastructure

Why this matters:
- the app now exports most of the right surfaces
- Phase 3 requires the monitoring system around those surfaces, not just the API endpoints

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- [logs.py](/home/user/projects/skynet/src/agp/logs.py)

### Gap 6: Shared Artifact Durability Is Stronger, But Still Not Cloud-Native
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
- checksum and existence validation are enforced before terminal state

Missing:
- an object-store or managed durable artifact backend
- explicit deployed redundancy assumptions for the artifact store
- store-level access policies
- restart/failure validation against the chosen hosted artifact backend

Why this matters:
- filesystem-backed registry semantics are useful
- Phase 3 still expects a production-grade hosted artifact story

Affected implementation:
- [artifact_store.py](/home/user/projects/skynet/src/agp/artifact_store.py)
- [research/artifact-and-finalization-spec.md](/home/user/projects/skynet/research/artifact-and-finalization-spec.md)

### Gap 7: Backup, Restore, And DR Are Still Local-First Relative To The Hosted Stack
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
- these workflows are still explicitly SQLite/filesystem oriented in [cli.py](/home/user/projects/skynet/src/agp/cli.py)

Missing:
- production-style Postgres backup integration
- production-style backup for the chosen shared artifact backend
- deployed-service recovery validation against PostgreSQL + Redis + shared artifacts
- RPO/RTO evidence

Why this matters:
- local DR correctness is not the same as hosted recovery evidence

Affected implementation:
- [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- [research/backup-restore-and-dr-spec.md](/home/user/projects/skynet/research/backup-restore-and-dr-spec.md)

### Gap 8: Upgrade And Rollout Semantics Still Need Deployed Service Choreography
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
- deployment-level rollout automation
- migration orchestration against the hosted dependency graph
- explicit runtime-fleet rollout strategy
- validated rollback drill in deployed environments

Why this matters:
- version metadata alone does not prove safe production rollout

Affected implementation:
- [control_plane.py](/home/user/projects/skynet/src/agp/control_plane.py)
- [cli.py](/home/user/projects/skynet/src/agp/cli.py)
- [research/upgrade-and-rollback-spec.md](/home/user/projects/skynet/research/upgrade-and-rollback-spec.md)

### Gap 9: Runbooks And Failure-Drill Evidence Are Still Partial
Phase 3 requires:
- full-environment deployment tests
- service restart and recovery tests
- state backup and restore validation
- artifact durability validation
- queue restart and redelivery validation
- runtime replacement and rollout tests
- observability and alert smoke tests
- failure-domain drills for control plane, queue, and state store
- credential rotation tests

Current state:
- AGP has strong platform-level drills and regression tests
- some operational helper scripts and smoke paths exist

Missing:
- full-environment deployment evidence package
- infrastructure-level control-plane, Postgres, and Redis outage drills
- documented proof that the RPO/RTO targets are met
- operator runbooks tied directly to the deployed topology

Why this matters:
- repo-level correctness is not the same thing as hosted operational proof

Affected implementation:
- [tests/test_mvp_flow.py](/home/user/projects/skynet/tests/test_mvp_flow.py)
- [scripts/validate_phase3_assets.py](/home/user/projects/skynet/scripts/validate_phase3_assets.py)
- [research/failure-injection-test-plan.md](/home/user/projects/skynet/research/failure-injection-test-plan.md)

## Priority

### Priority 0
- HA topology for core services
- live deployment validation on real infrastructure
- full-environment failure and recovery evidence

### Priority 1
- stronger service identity and transport security
- infrastructure-grade backup, restore, and rollout workflows
- deployed monitoring backend and dashboards

### Priority 2
- cloud-native object-store artifact backend
- richer executable runbooks and incident automation
- certificate/service-identity rotation integrated into deployment operations

## Required Deliverables
- HA topology definition for control plane, state store, and queue
- validated deployment smoke path for Compose and cluster deployment
- service identity and transport-security implementation plan
- metrics backend and dashboard definitions
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
