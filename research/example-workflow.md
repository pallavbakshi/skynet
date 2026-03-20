# Example Workflow

## Purpose
This document shows how the three AGP layers work together through one simple scenario. It is not a technical protocol. It is a product-level explanation of how intent moves through the system.

## Scenario
A human or higher-order orchestrator asks a reviewer agent to inspect a code change.

At the user level, the mental model is simple:

1. Send work to an agent
2. Get either a reply or a `job_id`
3. Track it if it takes time
4. Read the result
5. Hand it off or act on it

## Layer-by-Layer View

### Orchestration Layer
The orchestrator sends a message to a logical agent:

"Review this change and tell me if there are bugs or risks."

From the orchestrator's perspective, only a few things matter:
- which agent received the work
- whether the reply is immediate or long-running
- how to track progress if the work takes time
- where to read the result

If the work cannot complete immediately, the orchestrator receives a `job_id`.

The orchestrator then:
- watches the job
- waits for the result
- reads the review
- optionally hands the output to another agent

At this layer, the user should not need to think about queues, leases, or process recovery.

### Platform Layer
The platform receives the request and makes it dependable.

At this layer, the system:
- records the work request
- creates a job if the work is long-running
- assigns the work to a suitable runtime
- tracks one or more runs
- stores logs and outputs as artifacts
- records events and state changes
- handles failure and retry policy

If the underlying agent process stalls or crashes, the runtime attempts local recovery.

If local recovery is exhausted, the platform:
- updates the job state
- may retry or reassign the work
- preserves the record of what happened

The platform exists so the higher-level orchestration flow remains coherent even when execution is messy.

### Infrastructure Layer
The infrastructure layer hosts the platform and runtimes.

At this layer, the system provides:
- a machine or cluster where components run
- networking so platform and runtime can communicate
- persistent storage for state and artifacts
- restart behavior when services fail
- isolation and configuration delivery

Infrastructure does not decide what the review means. It simply provides a stable environment for the platform to operate.

## End-to-End Story

### 1. Intent Begins in Orchestration
The orchestrator decides to ask a reviewer agent for help.

### 2. The Platform Turns Intent Into Dependable Work
The platform accepts the request, decides it is long-running, and creates a `job_id`.

### 3. The Runtime Executes the Work
A runtime takes responsibility for the work, runs the reviewer agent, monitors it, and captures outputs.

### 4. Artifacts and State Are Recorded
The system stores the result, logs, and related outputs durably.

### 5. The Orchestrator Reads the Outcome
The orchestrator retrieves the result using the `job_id`, reads the review, and either:
- accepts it
- interrupts follow-up work
- hands it off to another agent

## Why This Workflow Matters
This example demonstrates the product contract between the layers:

- Orchestration stays simple
- Platform absorbs operational complexity
- Infrastructure remains replaceable underneath

That is the core AGP design goal.
