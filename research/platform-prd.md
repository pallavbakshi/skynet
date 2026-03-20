# Product Requirements Document

## Product Name
AGP Platform Layer

## Version
0.1 Draft

## Purpose
The Platform Layer is the operational core of the system. It accepts work, tracks truth, coordinates runtimes, stores outputs, and makes agentic execution dependable. This is the layer that turns unreliable agent CLIs and distributed workers into a coherent platform.

## Why This Exists
Raw agentic CLIs are not reliable enough to serve as infrastructure on their own. They hang, drift, crash, lose context, and fail in ambiguous ways. A serious multi-agent system needs a platform that can:

- hold the canonical state
- route work
- supervise execution
- persist outputs
- recover from failure

This layer exists to answer:

"How does the system reliably execute and track agent work?"

## Primary Users
- Runtime implementer
- Platform operator
- Orchestration layer
- Internal product teams building agent workflows

## Non-Technical Product Promise
The Platform Layer should make agent execution feel dependable even when the underlying agent process is not. It should absorb operational mess so higher-level orchestrators can think in logical terms instead of queue internals and process recovery.

## Core Responsibilities
- Maintain the source of truth for work and execution state
- Accept, store, and expose work requests
- Coordinate runtimes and agent availability
- Track execution attempts and ownership
- Persist large outputs separately from structured state
- Expose status, progress, and final results
- Recover from failures at the system level

## Out of Scope
- Machine hosting and low-level scheduling infrastructure
- Product backlog strategy
- Human task planning
- High-level orchestration policy
- Domain-specific prompting strategy

## Key Vocabulary
- Platform
  - The full backend layer that makes agent execution reliable
- Control Plane
  - The central coordinating service that tracks truth and exposes system APIs
- Runtime
  - The execution-side component that runs and supervises agents
- Agent
  - A logical worker identity that can perform certain kinds of work
- Capability
  - A declared type of work an agent or runtime can perform
- Job
  - A requested unit of work submitted to the platform
- Run
  - A single execution attempt of a job
- Lease
  - Temporary ownership of a run by a runtime or agent
- State Store
  - The durable store for structured system state
- Artifact
  - A durable large output or input blob such as logs, prompts, diffs, or results
- Artifact Store
  - The storage system used for artifacts
- Queue
  - The transport used to move work notifications between components
- Event
  - An immutable record of something that happened

## Product Principles
- State is authoritative
- Queues are transport, not truth
- Large outputs do not belong in the state store
- Runtimes own local recovery
- The control plane owns global coordination
- Platform APIs should feel simpler than platform internals

## Core Requirements
1. The platform must expose a single coherent source of truth for jobs and runs.
2. The platform must support distributed runtimes over a network.
3. The platform must make bounded retries and failure handling explicit.
4. The platform must separate structured state from large artifacts.
5. The platform must let higher layers reason in logical terms rather than execution details.

## User Stories
- As an orchestrator, I want to send work to a logical agent without managing queues or process state.
- As a runtime, I want to claim work, report progress, and escalate failures cleanly.
- As an operator, I want to inspect what is running, what failed, and what artifacts were produced.
- As a platform owner, I want the same core model to work locally and across multiple machines.

## Success Criteria
- Work can be submitted, executed, tracked, retried, and completed across distributed runtimes.
- The platform remains coherent when runtimes fail or disconnect.
- Large outputs are durable and retrievable without polluting the core state model.
- Higher-level orchestration can be built without exposing queue or lease mechanics directly.

## Risks
- Platform APIs may leak runtime or infrastructure details upward.
- The state model may become too execution-centric and hard for orchestrators to use.
- Runtime recovery may be too weak, forcing the control plane to handle local failure details.
- Artifact handling may be underdesigned, causing context overload later.

## Open Questions
- Should every message to an agent become a durable job, or only long-running work?
- Is work claimed pull-style by runtimes or pushed from the control plane?
- What is the minimum state model for v1: jobs, runs, leases, artifacts, events?
- How much local recovery autonomy should runtimes have before escalation?
