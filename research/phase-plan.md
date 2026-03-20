# AGP Phase Plan

## Purpose
This document explains the delivery sequence for AGP. It connects the master PRD and the three phase-based technical PRDs into one build plan.

The sequencing is intentional. Each phase de-risks a different class of problem:

- Phase 1 de-risks the core control loop
- Phase 2 de-risks distributed platform behavior
- Phase 3 de-risks full infrastructure bring-up

## Why AGP Is Sequenced This Way
AGP has three conceptual layers:
- Infrastructure
- Platform
- Orchestration

But it should not be built one layer at a time in isolation.

Instead, AGP should be built by proving the full product contract at increasing levels of operational seriousness:

1. first prove the control loop
2. then prove multi-runtime distributed behavior
3. then bring the full infrastructure online underneath the already-proven model

This order avoids two common failures:
- building infrastructure before the product model is stable
- building orchestration semantics that collapse under real runtime failure

## Phase Sequence

### Phase 1: Core Control Loop
Reference:
- [Phase 1 Technical PRD](/home/user/projects/skynet/phase-1-technical-prd.md)

Objective:
- prove that AGP can accept work, assign it to a runtime, supervise execution, persist outputs, and expose a simple orchestration surface

What it validates:
- the core vocabulary
- the message-to-job model
- the runtime supervision contract
- the separation between state store, queue, and artifact store
- the minimum viable control plane API

Why it comes first:
- if this loop is ambiguous or fragile on one machine, distributing it will only magnify the problem

Key output of the phase:
- a working end-to-end AGP core that can reliably run long-running agent work in a small trusted environment

### Phase 2: Distributed Platform Behavior
Reference:
- [Phase 2 Technical PRD](/home/user/projects/skynet/phase-2-technical-prd.md)

Objective:
- prove that AGP remains coherent when multiple runtimes operate across a network

What it validates:
- capability-based routing
- distributed claiming and lease semantics
- runtime health classification
- reassignment after failure
- handoff and artifact chaining between jobs
- richer inspection and operator visibility

Why it comes second:
- once the control loop is correct, the next risk is distributed coordination
- this is the phase where AGP stops being a local prototype and becomes a genuine platform

Key output of the phase:
- a networked multi-runtime AGP platform with stable orchestration semantics

### Phase 3: Full Infrastructure Bring-Up
Reference:
- [Phase 3 Technical PRD](/home/user/projects/skynet/phase-3-technical-prd.md)

Objective:
- operationalize AGP as a full hosted system with durable infrastructure underneath it

What it validates:
- deployment architecture
- shared persistence
- service-to-service networking
- secrets and configuration management
- restart and rollout behavior
- observability, monitoring, backup, and restore

Why it comes third:
- infrastructure should serve the product model, not define it
- by this point, AGP's platform and orchestration semantics should already be stable enough to host seriously

Key output of the phase:
- the entire AGP infrastructure up and running, with all major services operational together

## What Each Phase De-Risks

### Phase 1 De-Risks
- incorrect core vocabulary
- broken control-plane/runtime contract
- weak runtime supervision model
- confusion between immediate replies and durable jobs
- poor artifact boundaries

### Phase 2 De-Risks
- broken distributed claiming
- weak failure and reassignment behavior
- capability routing ambiguity
- orchestration collapse under multi-agent coordination
- poor operator visibility at platform scale

### Phase 3 De-Risks
- infrastructure-hosting mismatch
- weak service restart behavior
- poor persistence durability
- operational blind spots
- deployment complexity overwhelming the platform model

## Relationship Between Conceptual Layers and Delivery Phases
The phases are not a direct one-to-one mapping to the layers.

Instead:
- Phase 1 touches platform and orchestration first, with minimal infrastructure assumptions
- Phase 2 deepens platform and orchestration under distributed conditions
- Phase 3 completes the infrastructure layer beneath the already-proven platform model

This is deliberate. AGP should be product-shaped first, then infrastructure-complete.

## Completion Logic
AGP should be considered mature across phases only when:

1. the orchestration surface is simple and coherent
2. the platform behaves reliably under runtime failure
3. the infrastructure can host the system durably across machines

If any of those remain weak, the phase sequence has not actually succeeded.

## Related Documents
- [Master PRD](/home/user/projects/skynet/master-prd.md)
- [Phase 1 Technical PRD](/home/user/projects/skynet/phase-1-technical-prd.md)
- [Phase 2 Technical PRD](/home/user/projects/skynet/phase-2-technical-prd.md)
- [Phase 3 Technical PRD](/home/user/projects/skynet/phase-3-technical-prd.md)
- [Job / Run / Lease Protocol](/home/user/projects/skynet/job-run-lease-protocol.md)
- [State Machine Spec](/home/user/projects/skynet/state-machine-spec.md)
- [Control Plane API Spec](/home/user/projects/skynet/control-plane-api-spec.md)
- [Data Model Spec](/home/user/projects/skynet/data-model-spec.md)
- [Artifact and Finalization Spec](/home/user/projects/skynet/artifact-and-finalization-spec.md)
- [Agent Lifecycle Spec](/home/user/projects/skynet/agent-lifecycle-spec.md)
- [Implementation Backlog](/home/user/projects/skynet/implementation-backlog.md)
