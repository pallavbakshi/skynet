# AGP Product Brief

## One-Line Summary
AGP is a layered system for reliable agent execution and coordination.

## Short Description
AGP lets humans and higher-order agents work with logical agents through a simple coordination model while the underlying platform handles execution reliability and the infrastructure provides stable hosting.

In practical terms, AGP aims to make this interaction feel simple:

- send work to an agent
- get either a reply or a `job_id`
- track progress if it takes time
- retrieve the result and related artifacts

## The Core Problem
Agentic systems often collapse too many responsibilities into one place. The same system is expected to coordinate work, supervise flaky agent processes, manage state, and deal with infrastructure failures. That makes the experience fragile and hard to reason about.

## AGP's Approach
AGP separates the system into three layers:

- `Infrastructure`
  - hosts the system reliably
- `Platform`
  - makes agent execution dependable
- `Orchestration`
  - gives humans and agents a simple logical coordination surface

This separation allows AGP to stay mentally simple at the top while remaining operationally robust underneath.

## Who It Is For
- Human operators coordinating agent work
- Higher-order orchestrator agents
- Teams building reliable agent workflows
- Other AI systems that want to use AGP as a substrate

## Product Promise
AGP should let users focus on agents, messages, jobs, and outcomes rather than queues, restarts, and infrastructure details.

## Why It Matters
If AGP works as intended, users should be able to trust the system to:
- handle long-running work cleanly
- survive runtime instability
- preserve outputs and system history
- scale from a single machine to a distributed deployment

## Non-Goals
AGP is not tied to:
- one specific hosting substrate
- one specific model provider
- one specific orchestration style

## Related Documents
- [Master PRD](/home/user/projects/skynet/master-prd.md)
- [Glossary](/home/user/projects/skynet/glossary.md)
- [Example Workflow](/home/user/projects/skynet/example-workflow.md)
