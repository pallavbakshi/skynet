# Product Requirements Document

## Product Name
AGP Infrastructure Layer

## Version
0.1 Draft

## Purpose
The Infrastructure Layer exists to host the platform reliably across one or more computers. It is the physical substrate beneath the agent platform. Its job is not to understand agents, jobs, or orchestration logic. Its job is to provide dependable compute, networking, storage, and recovery primitives so the platform can run.

## Why This Exists
Agent systems are noisy, long-running, and failure-prone. Machines reboot, pods die, networks flap, disks fill, and processes disappear. If the hosting layer is weak or inconsistent, the agent platform becomes fragile no matter how well the higher layers are designed.

This layer exists to answer a simple question:

"Where and how does the system run safely and reliably?"

## Primary Users
- Platform operator
- Infrastructure/SRE owner
- System administrator

## Non-Technical Product Promise
The Infrastructure Layer should make the overall system feel stable, replaceable, and portable. The platform should be able to run on a laptop, a few servers, or Kubernetes without changing its core meaning.

## Core Responsibilities
- Provide compute where platform services and runtimes can run
- Provide network connectivity between distributed components
- Provide persistent storage for state and artifacts
- Provide secrets and configuration delivery
- Provide restart and placement behavior for failed services
- Provide environment isolation between workloads

## Out of Scope
- Agent-to-agent coordination
- Job semantics
- Business logic for retries or delegation
- Prompt handling
- Artifact meaning
- Orchestrator workflows

## Key Vocabulary
- Infrastructure
  - The underlying hosting environment for the system
- Node
  - A machine or host capable of running workloads
- Workload
  - A deployable software unit such as a process, service, or container
- Network
  - The connectivity layer that allows components to communicate
- Volume
  - Durable mounted storage
- Secret
  - Sensitive configuration data such as tokens or keys
- Placement
  - Where a workload is scheduled to run
- Restart
  - Automatic recovery of failed workloads

## Product Principles
- Invisible when healthy
- Replaceable underneath the platform
- Boring and predictable
- Strong isolation by default
- Durable storage for critical state

## Core Requirements
1. The system must support running across multiple computers.
2. Platform components must be restartable without redefining product semantics.
3. Stateful and stateless workloads must be distinguishable.
4. Storage and networking must be explicit platform dependencies, not hidden assumptions.
5. The layer must support both local development and distributed deployment.

## User Stories
- As an operator, I want to restart a failed platform service without losing the meaning of in-flight work.
- As an operator, I want to move workloads between machines without changing the agent platform API.
- As an operator, I want clear boundaries between persistent state and disposable runtime state.
- As a platform owner, I want to host the system locally first and later move it to Kubernetes if needed.

## Success Criteria
- The platform can run on one machine for development and multiple machines in production.
- Infrastructure failures do not force a redesign of platform abstractions.
- Platform services can be restarted, rescheduled, or replaced with limited operator intervention.
- Storage, networking, and secrets are managed consistently.

## Risks
- Infrastructure concepts may leak upward into the platform API.
- Hosting choices may become coupled to the domain model too early.
- Local development and distributed deployment may drift into separate architectures.

## Open Questions
- What is the minimum supported deployment shape for v1: local process, VM, or containerized service?
- Which parts of the system require durable storage on day one?
- What level of isolation is needed between runtimes in the first release?
- When does Kubernetes become justified operationally rather than aspirationally?
