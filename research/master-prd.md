# Product Requirements Document

## Product Name
AGP: Agentic Plane

## Version
0.1 Draft

## Purpose
AGP is a layered system for reliable agent execution and coordination. Its purpose is to let humans and higher-order agents work with logical agents at a simple, understandable level while the underlying platform handles execution reliability and the infrastructure provides stable hosting.

AGP is not just an agent runtime and not just an orchestration tool. It is the full product surface that connects:

- reliable hosting
- dependable execution
- logical agent coordination

## Vision
Current agentic systems often collapse too many responsibilities into one place. The same system or model is expected to plan, delegate, execute, monitor, recover from failure, and manage infrastructure concerns at the same time. That leads to fragility, context overload, and poor operational clarity.

AGP exists to separate these concerns cleanly.

At a high level:
- the Infrastructure Layer hosts the system
- the Platform Layer makes agent execution reliable
- the Orchestration Layer lets humans and agents coordinate work simply

The product goal is not maximum complexity. The goal is a system that feels operationally trustworthy while remaining mentally simple.

## Product Promise
AGP should let a user think:

"I can send work to an agent, track what is happening, get the result, and trust the system to handle the messy parts underneath."

That promise depends on three different layers working together without forcing the user to think like an infrastructure operator or runtime engineer.

## The Three Layers

### 1. Infrastructure Layer
The Infrastructure Layer is the hosting substrate for the system.

It exists to answer:

"Where and how does the system run safely and reliably?"

It is responsible for:
- compute
- networking
- storage
- restart behavior
- isolation
- configuration and secrets delivery

It is not responsible for understanding agents, jobs, or orchestration logic.

### 2. Platform Layer
The Platform Layer is the operational core of AGP.

It exists to answer:

"How does the system reliably execute and track agent work?"

It is responsible for:
- control-plane coordination
- runtime supervision
- durable state
- work distribution
- execution tracking
- artifact persistence
- recovery from failure

It turns unreliable agentic CLIs and distributed workers into something dependable enough to build on.

### 3. Orchestration Layer
The Orchestration Layer is the logical coordination surface used by humans and higher-order agents.

It exists to answer:

"How do humans and orchestrators coordinate agent work at a logical level?"

It is responsible for:
- sending work to logical agents
- handling immediate replies and long-running jobs
- tracking status and outcomes
- routing outputs between agents
- supporting interruption and redirection

It should feel simple even when the layers underneath are operationally complex.

## Why This Layering Matters
Each layer solves a different class of problem.

Without this separation:
- infrastructure details leak into the user experience
- orchestration logic becomes coupled to process supervision
- the platform becomes shaped by hosting choices instead of product needs

With this separation:
- hosting can evolve without redefining the product
- runtimes can improve without changing orchestration semantics
- orchestration can stay focused on goals, agents, and outcomes

## Who This Product Is For
- Human operators coordinating agent work
- Higher-order orchestrator agents
- Runtime implementers building reliable execution environments
- Platform operators responsible for keeping the system healthy
- Other AI systems that want to use AGP as an execution and coordination substrate

## Core Product Principles
- Keep the user-facing model simple
- Keep system truth explicit and durable
- Separate logical coordination from execution mechanics
- Separate execution mechanics from hosting concerns
- Make long-running work visible and trackable
- Keep large outputs available without overwhelming the user-facing layer
- Make human override first-class

## Shared Vocabulary
The canonical vocabulary for AGP lives in:

- [Glossary](/home/user/projects/skynet/glossary.md)

The summary below exists for readability. The glossary is the source of truth.

### Top-Level Terms
- AGP
  - The overall product made up of the infrastructure, platform, and orchestration layers
- Layer
  - A bounded level of responsibility in the overall system

### Platform and Execution Terms
- Control Plane
  - The coordinating core of the platform that tracks truth and exposes system APIs
- Runtime
  - The execution-side part of the platform that runs and supervises agents
- Agent
  - A logical worker that can perform certain kinds of work
- Capability
  - A declared kind of work an agent can perform
- Job
  - A requested unit of work
- Run
  - A single execution attempt of a job
- Lease
  - Temporary ownership of a run by a runtime or agent
- Event
  - An immutable record of something that happened

### Output and State Terms
- State Store
  - The durable source of truth for structured system state
- Artifact
  - A durable large input or output such as logs, prompts, results, diffs, or screenshots
- Artifact Store
  - The durable store used for artifacts
- Queue
  - The transport used to move work notifications between components

### Orchestration Terms
- Orchestrator
  - A human or agent coordinating work between agents
- Message
  - A request or instruction sent to an agent
- Reply
  - A direct response to a message
- Job ID
  - The handle used to track long-running work created from a message
- Handoff
  - Routing responsibility or output from one agent to another
- Interrupt
  - A request to stop or redirect active work

## Mental Model for Users
The simplest mental model of AGP is:

1. Send work to an agent
2. Get either an immediate reply or a `job_id`
3. Track progress if the work takes time
4. Read the result and associated artifacts
5. Redirect, interrupt, or hand off work as needed

The user should not have to think about:
- queue internals
- lease mechanics
- runtime process supervision
- machine placement

Those concerns belong to the layers underneath.

See also:
- [Example Workflow](/home/user/projects/skynet/example-workflow.md)

## Mental Model for the System
Internally, the product works as a chain:

1. The Orchestration Layer expresses intent
2. The Platform Layer turns that intent into reliable execution
3. The Infrastructure Layer provides the environment in which the platform runs

This means:
- orchestration is logical
- platform is operational
- infrastructure is physical

## Boundaries Between Layers

### Infrastructure to Platform
Infrastructure provides the environment. Platform defines the product semantics.

Infrastructure should not define:
- what a job means
- what an agent is
- how work is coordinated

### Platform to Orchestration
Platform provides dependable primitives. Orchestration defines the user-facing coordination experience.

Platform should not force:
- one specific planning model
- one specific orchestration policy
- one specific kind of agent team structure

Orchestration should not need to understand:
- queue behavior
- runtime recovery steps
- storage internals

## Core User Experience Goals
- A user can send work to a logical agent without needing system internals.
- Long-running work is easy to track and inspect.
- Results are durable and routable between agents.
- Failures are visible without exposing unnecessary backend complexity.
- The system can grow from local usage to distributed usage without changing its core meaning.

## Product Scope
AGP includes:
- a layered product model
- dependable execution primitives
- durable work and output tracking
- logical agent coordination

AGP does not require:
- one specific hosting substrate
- one specific model provider
- one specific orchestration style

## Success Criteria
- The three layers remain conceptually distinct.
- Operators can host AGP in different environments without redefining product semantics.
- Runtime instability does not dominate the orchestration user experience.
- Humans and higher-order agents can coordinate work using simple logical concepts.
- The system remains understandable as it grows from a single-machine setup to a distributed deployment.

## Risks
- Terms may blur across layers and create conceptual drift.
- Platform concerns may leak into orchestration and overload the user experience.
- Infrastructure choices may harden too early and distort the product model.
- The product may become too abstract if the vocabulary is not kept disciplined.

## Relationship to Layer PRDs
This master PRD defines the overall product and shared language.

The layer-specific PRDs define the role of each layer in more detail:
- [Infrastructure PRD](/home/user/projects/skynet/infrastructure-prd.md)
- [Platform PRD](/home/user/projects/skynet/platform-prd.md)
- [Orchestration PRD](/home/user/projects/skynet/orchestration-prd.md)

Supporting documents:
- [Glossary](/home/user/projects/skynet/glossary.md)
- [Example Workflow](/home/user/projects/skynet/example-workflow.md)
- [Product Brief](/home/user/projects/skynet/product-brief.md)

## Relationship to Phase PRDs
The layer PRDs describe the system conceptually. The phase PRDs describe how AGP should be built and operationalized over time.

Phase documents:
- [Phase Plan](/home/user/projects/skynet/phase-plan.md)
- [Phase 1 Technical PRD](/home/user/projects/skynet/phase-1-technical-prd.md)
- [Phase 2 Technical PRD](/home/user/projects/skynet/phase-2-technical-prd.md)
- [Phase 3 Technical PRD](/home/user/projects/skynet/phase-3-technical-prd.md)

The intended relationship is:
- layer PRDs define responsibilities and boundaries
- the master PRD defines the overall product and shared vocabulary
- phase PRDs define delivery order, technical scope, and implementation depth

## Relationship to Authoritative Specs
The following documents are the implementation-authoritative specifications for AGP core behavior:

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

Operational readiness specifications:
- [Deployment Architecture Spec](/home/user/projects/skynet/deployment-architecture-spec.md)
- [Security Model Spec](/home/user/projects/skynet/security-model-spec.md)
- [Observability Spec](/home/user/projects/skynet/observability-spec.md)
- [Backup, Restore, and DR Spec](/home/user/projects/skynet/backup-restore-and-dr-spec.md)
- [Upgrade and Rollback Spec](/home/user/projects/skynet/upgrade-and-rollback-spec.md)
- [Failure Injection Test Plan](/home/user/projects/skynet/failure-injection-test-plan.md)

Derived implementation artifacts:
- [OpenAPI Stub](/home/user/projects/skynet/openapi.yaml)
- [Initial DB Migration Stub](/home/user/projects/skynet/migrations/0001_initial.sql)
- [Implementation Backlog](/home/user/projects/skynet/implementation-backlog.md)

## Open Questions
- Should every user-facing message become a durable job, or only long-running work?
- How much of agent identity should be stable versus dynamically provisioned?
- Does orchestration remain message-first, or eventually become session-first?
- At what point does distributed hosting become a practical need rather than a design assumption?
