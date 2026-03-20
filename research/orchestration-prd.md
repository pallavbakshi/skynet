# Product Requirements Document

## Product Name
AGP Orchestration Layer

## Version
0.1 Draft

## Purpose
The Orchestration Layer is the logical coordination surface used by humans and higher-order agents. It decides who should do what, sends requests to agents, tracks long-running work, and routes outputs between participants. It should think in terms of goals and delegation, not queues and process supervision.

## Why This Exists
Even a reliable platform is not enough by itself. Someone or something still has to decide:

- which agent should handle a request
- when to wait versus delegate
- how to track long-running work
- how to combine outputs from multiple agents
- when to interrupt or redirect effort

This layer exists to answer:

"How do humans and orchestrators coordinate agent work at a logical level?"

## Primary Users
- Human operator
- Executive/orchestrator agent
- Product owner or planning agent
- Other AI systems using the platform as a service

## Non-Technical Product Promise
The Orchestration Layer should let users think in terms of agents, messages, replies, and outcomes. It should hide infrastructure and most platform mechanics while still giving enough visibility to manage work confidently.

## Core Responsibilities
- Address work to logical agents
- Support direct messages and long-running delegated work
- Return immediate replies when possible
- Return trackable job handles when work is asynchronous
- Provide status, progress, and artifact access in a way that fits orchestration workflows
- Support interruption, redirection, and human override
- Provide a coherent mental model for multi-agent collaboration

## Out of Scope
- Host-level reliability
- Runtime process recovery
- Queue implementation
- Low-level state persistence
- Storage and networking details

## Key Vocabulary
- Orchestration
  - The logical coordination of agent work
- Orchestrator
  - A human or agent that delegates, tracks, and routes work
- Agent
  - A logical participant that can receive work and produce results
- Message
  - A request or instruction sent to an agent
- Reply
  - A direct response to a message
- Job
  - A trackable unit of long-running work created when a message cannot complete immediately
- Job ID
  - The handle used to watch and retrieve the outcome of asynchronous work
- Artifact
  - A durable output referenced by orchestration results
- Interrupt
  - A request to stop or redirect active work
- Handoff
  - Routing output or responsibility from one agent to another

## Product Principles
- Think in logical agents, not machines
- Default to simple messaging semantics
- Long-running work should become explicitly trackable
- Outputs should be easy to route between agents
- Human override must remain first-class
- Visibility matters, but the user should not need platform internals

## Core Requirements
1. The layer must allow sending work to logical agents without exposing queue mechanics.
2. The layer must support both immediate replies and asynchronous job tracking.
3. The layer must make multi-agent delegation understandable.
4. The layer must expose artifacts and outcomes in operator-friendly form.
5. The layer must support interruption and redirection of work.

## User Stories
- As an orchestrator, I want to send a message to an agent and either get an immediate reply or a `job_id` I can track.
- As a human operator, I want to interrupt active work and redirect effort without understanding runtime internals.
- As an orchestrator, I want to pass one agent's result to another agent cleanly.
- As a user of the system, I want long-running work to remain visible and inspectable.

## Success Criteria
- Users can reason in terms of agents, messages, jobs, and artifacts.
- Long-running work is easy to track without exposing transport and storage internals.
- Multi-agent coordination is understandable enough to be used repeatedly, not just demoed.
- Human intervention can redirect work without collapsing system coherence.

## Risks
- Orchestration UX may become overloaded with backend terminology.
- Messaging semantics may be too thin to support richer collaboration later.
- The line between reply, job, and artifact may become blurry.
- The orchestration layer may accidentally depend on one specific runtime style.

## Open Questions
- Should orchestration be message-first, with jobs created only when needed?
- What is the minimum operator surface: send, watch, interrupt, fetch?
- Does the orchestration layer need first-class sessions or can it start message/job-first?
- How much agent identity should be stable versus dynamically provisioned?
