# AGP Queue Topology and Routing Specification

## Status
Authoritative

## Purpose
Defines queue topology, routing precedence, FIFO guarantees, tie-breaking, and redelivery assumptions.

## Queue Types
- `agent queue`
  - Dedicated FIFO queue owned by one durable agent
- `capability pool queue`
  - Shared queue for jobs addressed to a capability rather than a named agent

## Routing Rules
- A job targets exactly one queue at a time.
- `target.type=agent` routes to that agent's queue.
- `target.type=capability` routes to that capability pool queue.

## Precedence
- Explicit agent target always wins over capability routing.
- Capability pool routing is used only when no concrete agent target is specified.

## FIFO Guarantees
- Strict FIFO applies within a single agent queue.
- Capability pool queues are FIFO at dequeue order, subject to runtime eligibility and claim timing.
- No global FIFO exists across queues.

## Tie-Breaking
For capability-pool work, if multiple runtimes are eligible:
1. exclude runtimes whose `health_status` is `unreachable`
2. exclude runtimes whose `status` is `draining`
3. prefer `healthy` over `degraded`
4. prefer runtimes already hosting an eligible warm agent if policy allows
5. otherwise deterministic order by control-plane policy, default lexical `runtime_id`

## Queue Redelivery Assumptions
- Delivery is at-least-once.
- Redelivery is expected after consumer crash, timeout, or broker restart.
- Queue redelivery never overrides state-store truth.
- Deduplication is enforced through run/lease/fencing semantics, not by trusting broker uniqueness.

## Agent Queue Ownership
- Each durable agent owns exactly one queue.
- Queue ownership ends only when the agent is terminated.
- Force-down does not silently transfer queue ownership; queued jobs are cancelled unless re-dispatched.

## Capability Pool Behavior
- Capability pools may create new agent bindings or reuse existing eligible agents.
- Once a job is claimed, execution always happens under a concrete agent identity.

## Invalid Routing
- A terminated agent cannot receive new work.
- A draining agent cannot receive new work.
- A capability with no eligible runtime remains queued until eligibility changes or policy fails it.
