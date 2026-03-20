# AGP Glossary

## Purpose
This document is the canonical vocabulary reference for AGP. Its purpose is to keep the product language consistent across the Infrastructure, Platform, and Orchestration PRDs.

Each term should have one primary meaning. If a term does not need to exist yet, it should not be introduced casually in other docs.

## Canonical Terms

### AGP
The overall product made up of the Infrastructure, Platform, and Orchestration layers.

### Layer
A bounded level of responsibility in the AGP system.

### Infrastructure
The hosting environment beneath the platform. It provides compute, networking, storage, restart behavior, and isolation.

### Platform
The operational core of AGP. It makes agent execution dependable by coordinating work, tracking state, supervising runtimes, and storing outputs.

### Orchestration
The logical coordination layer used by humans and higher-order agents to delegate work, track outcomes, and route results.

### Control Plane
The coordinating core of the platform. It tracks truth, exposes APIs, records state, and coordinates execution.

### Runtime
The execution-side part of the platform. It runs and supervises agents and reports progress and outcomes back to the control plane.

### Agent
A logical worker identity that can perform certain kinds of work.

### Capability
A declared kind of work an agent can perform.

### Message
A request or instruction sent to an agent at the orchestration layer.

### Reply
A direct response to a message.

### Job
A requested unit of work tracked by the platform. A message may create a job when the requested work is not immediate.

### Job ID
The handle used to track a long-running job.

### Run
A single execution attempt of a job.

### Lease
Temporary ownership of a run by a runtime or agent.

### Event
An immutable record of something that happened in the system.

### State Store
The durable source of truth for structured system state.

### Artifact
A durable large input or output such as a prompt, log, result, diff, or screenshot.

### Artifact Store
The durable store used for artifacts.

### Queue
The transport used to move work notifications between components. The queue is not the source of truth.

### Orchestrator
A human or higher-order agent that coordinates work between agents.

### Handoff
Routing work, responsibility, or output from one agent to another.

### Interrupt
A request to stop or redirect active work.

## Terms To Use Carefully

### Worker
Avoid as a primary term unless it is clearly distinguished from `agent` and `runtime`. In AGP, `agent` is the logical worker identity and `runtime` is the execution-side component.

### Task
Avoid as a primary term in core docs. Use `job` unless there is a strong reason to distinguish the two.

### Session
Not yet a canonical term. Introduce only if AGP becomes explicitly conversation-first.

### Turn
Not yet a canonical term. Introduce only if AGP adds first-class conversational sequencing.

### Node / Pod / Container
Infrastructure terms only. They should not leak into platform or orchestration docs unless the discussion is explicitly about hosting.

## Usage Rules
- Use `job` and `run` together, not interchangeably.
- Use `message` and `job` distinctly: a message is an orchestration request; a job is a platform-tracked work unit.
- Use `runtime` and `control plane` as parts of the platform, not as synonyms for the whole platform.
- Use `artifact` for large durable payloads, not `log`, `blob`, or `file` as a generic substitute.
- Prefer `agent` for the logical participant seen by the orchestrator.
