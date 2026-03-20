# AGP Event Model Specification

## Status
Authoritative

## Purpose
Defines event types, payload classes, ordering guarantees, causality links, emitters, and retention expectations.

## Ordering
- `event_seq` assigned by control plane is the only authoritative ordering key
- timestamps are informational only

## Event Categories
- `job lifecycle`
- `run lifecycle`
- `lease lifecycle`
- `agent lifecycle`
- `runtime lifecycle`
- `artifact lifecycle`
- `handoff lifecycle`

## Required Event Types
- `job.accepted`
- `job.queued`
- `job.requeued`
- `job.completed`
- `job.failed`
- `job.cancelled`
- `run.created`
- `run.running`
- `run.progress`
- `run.completed`
- `run.failed`
- `run.cancelled`
- `run.abandoned`
- `lease.acquired`
- `lease.heartbeat`
- `lease.expired`
- `lease.released`
- `agent.provisioning`
- `agent.idle`
- `agent.busy`
- `agent.draining`
- `agent.terminated`
- `runtime.registered`
- `runtime.degraded`
- `runtime.offline`
- `artifact.created`
- `handoff.created`

## Payload Shape
Every event includes:
- `event_id`
- `event_seq`
- `event_type`
- `created_at`
- optional linkage:
  - `job_id`
  - `run_id`
  - `agent_id`
  - `runtime_id`
- `body`

## Required Event Body Fields

### `job.accepted`
- `message_id`
- `target_type`
- `target_id`

### `job.queued`
- `target_queue`

### `job.requeued`
- `reason`
- `retry_count`

### `run.created`
- `attempt`

### `run.running`
- `started_by`

### `run.progress`
- `message`
- optional `payload`

### `lease.acquired`
- `lease_id`
- `fencing_token`
- `expires_at`

### `lease.expired`
- `lease_id`
- `reason`

### `run.completed`
- `artifact_ids`

### `run.failed`
- `artifact_ids`

### `run.cancelled`
- `reason`

### `handoff.created`
- `handoff_id`
- `source_job_id`
- `source_artifact_ids`
- `created_job_ids`

## Causality Rules
- events may reference one or more entities
- causality is established by entity linkage plus event sequence
- handoff events must link source job and created child jobs

## Emission Rules
- control plane emits authoritative lifecycle events
- runtimes do not write directly to the event log; they trigger event creation through accepted API calls

## Retention and Audit
- events are durable audit records
- events should be retained at least as long as operational audit requirements demand
- terminal-state events must never be deleted before associated state retention expires
