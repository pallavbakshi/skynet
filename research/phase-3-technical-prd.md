# Technical PRD

## Document
AGP Phase 3 Technical PRD

## Version
0.1 Draft

## Purpose
Phase 3 operationalizes AGP as a fully hosted infrastructure-backed system. By the end of this phase, the platform and orchestration layers should run on production-style infrastructure with deployment automation, shared storage, service discovery, secrets management, and operational observability.

Phase 3 is where the entire infrastructure is up and running underneath the already-proven product model.

## Why Phase 3 Exists
AGP only becomes a durable platform product when its infrastructure is no longer incidental. By this point, the product and platform semantics should already be stable enough that infrastructure choices can serve them rather than distort them.

This phase exists to answer:
- can AGP run as a real hosted system across multiple computers?
- can the infrastructure support stable operation, restartability, and scaling?
- can platform and orchestration semantics remain unchanged while deployment becomes more serious?

## Phase Goal
By the end of Phase 3, AGP should be deployable as a complete distributed system with production-style infrastructure, shared service dependencies, and operator-grade observability and recovery.

## In Scope
- Full deployment architecture
- Shared networked state store
- Shared durable artifact backend
- Queue deployment and operation
- Secrets and configuration management
- Service-to-service networking
- Deployment automation
- Health checks and monitoring
- Runtime rollout and restart strategy
- Backup and restore for state and artifacts
- Infrastructure operator runbooks
- HA policy for core services
- Observability baseline with metrics, traces, logs, and alerts
- Authentication and authorization boundaries for platform services

## Out of Scope
- Global multi-region failover
- Tenant billing
- Advanced policy engines
- Full session/turn-based conversational model
- Model-specific optimization beyond runtime adapter concerns

## Infrastructure Target
Phase 3 assumes AGP can be hosted on real infrastructure such as:
- Kubernetes
- managed VMs with service supervision
- comparable clustered deployment substrate

The key requirement is not one specific technology. The key requirement is that AGP now has a complete and operational infrastructure footprint.

## Deployment Architecture

### Required Deployed Services
- `control-plane`
- `runtimes`
- `state-store`
- `artifact-store`
- `queue`
- `operator-facing monitoring and logging components`

### Required Infrastructure Capabilities
- internal service networking
- durable storage
- restart and rescheduling behavior
- rolling deploy support
- secrets injection
- environment configuration management
- health checking
- observability
- backup and restore
- service identity

## Functional Infrastructure Requirements

### 1. Service Hosting
The infrastructure must host all AGP services as durable workloads with explicit restart behavior.

### 2. Shared Persistence
The infrastructure must provide:
- durable relational storage for system state
- durable artifact storage for prompts, transcript logs, exec logs, results, and failure evidence
- backup and restore procedures
- artifact immutability or equivalent write-once semantics after finalization

### 3. Networking
The infrastructure must provide:
- stable service discovery
- secure service-to-service communication
- routable paths for runtimes on different machines

### 4. Operational Visibility
The infrastructure must provide:
- service health visibility
- logs and metrics
- distributed traces
- runtime fleet visibility
- alertable failure conditions

### 5. Deployment Safety
The infrastructure must support:
- restart without semantic corruption
- service rollout without data loss
- runtime replacement without redefining platform state

## Platform-on-Infrastructure Contract
By Phase 3, the infrastructure must support the following platform invariants:
- state truth survives service restart
- queue loss does not erase job truth
- artifact references remain durable
- runtime churn does not corrupt jobs or runs
- orchestration semantics do not change because of hosting choice
- expired owners cannot write authoritative terminal state after fencing

## HA and Recovery Targets
- `control-plane`, `state-store`, and `queue` must be highly available.
- `artifact-store` must be durable and operationally redundant even if exposed through a managed service.
- `runtimes` are intentionally ephemeral and do not require HA as individual instances.
- Target RPO:
  - near-zero for control-plane state and backlog data
  - up to one minute for replayable active job progress
- Target RTO:
  - under 5 seconds for control-plane routing recovery
  - 10 to 60 seconds for runtime reprovisioning and agent capability recovery

## Security Requirements
- Secrets must not be stored in plain application configuration.
- Service identity and configuration boundaries must be explicit.
- Artifact and state backends must have access control boundaries.
- Runtime-to-control-plane communication must be authenticated within the deployment boundary.
- Encryption in transit must be enforced for service-to-service communication.
- Operator access to state and artifacts must be controlled and auditable.
- Service credentials and certificates must be rotatable.

## Reliability Requirements
- Control plane must be restartable without losing authoritative state.
- Runtimes must be individually replaceable.
- Shared stores must survive workload churn.
- Monitoring must detect runtime loss, control-plane degradation, and backend dependency failure.
- Queue redelivery must not violate job truth because the state store remains authoritative.
- Backup and restore must preserve state-to-artifact references.

## Queue and Persistence Semantics
- Queue delivery may be at-least-once; deduplication and lease correctness are enforced at the platform layer.
- Queue redelivery must be expected during restart and recovery.
- State-store records remain authoritative for jobs, runs, leases, events, and artifact references.
- Artifacts must be durable and immutable once finalized.
- Backup and restore procedures must preserve both database state and artifact availability.

## Observability Baseline
Production readiness requires at minimum:
- control-plane metrics for API latency, job queue depth, lease churn, and infrastructure-adapter success/failure rates
- runtime metrics for heartbeats, local recovery attempts, crash counts, and artifact upload failures
- structured transcript and exec logs for agent execution
- distributed traces from `send` to queue to claim to run to artifact finalization to terminal state
- alerts for control-plane outage, queue outage, state-store outage, artifact-store outage, heartbeat loss spikes, and repeated runtime fencing

## Upgrade and Rollback Requirements
- The deployment model must support rolling upgrade for stateless services.
- Schema migration strategy must be defined before production rollout.
- Version skew tolerance between control plane and runtimes must be explicit.
- Rollback procedures must preserve state-store correctness and artifact compatibility.

## Operational Deliverables
- Deployment manifests or equivalent automation
- Environment configuration model
- Secrets management plan
- Backup and restore procedures
- Monitoring dashboard definitions
- Health check endpoints and alert conditions
- Runbooks for runtime failure, control-plane failure, and storage incidents
- HA topology documentation for core services
- RPO/RTO and recovery procedure documentation

## Acceptance Criteria
- AGP can be deployed as a complete system across multiple computers.
- All required services have durable hosting, networking, and persistence.
- Control plane, runtimes, queue, state store, and artifact store are all operational together.
- Operators can restart services, replace runtimes, and recover from failures without redefining system semantics.
- The orchestration experience remains stable despite the move to production-style infrastructure.
- Core HA services meet the documented RPO/RTO targets in failure drills.
- Observability baseline is deployed and demonstrates end-to-end traceability.

## Test Strategy
- Full-environment deployment tests
- Service restart and recovery tests
- State backup and restore validation
- Artifact durability validation
- Queue restart and redelivery validation
- Runtime replacement and rollout tests
- Observability and alert smoke tests
- Failure-domain drills for control plane, queue, and state store
- Certificate/credential rotation tests

## Exit Criteria
Phase 3 is complete when AGP runs as a full infrastructure-backed platform, with all major services deployed, connected, observable, restartable, and operationally supportable.
