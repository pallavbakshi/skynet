# AGP Data Model Specification

## Status
Authoritative

## Purpose
This document defines the authoritative AGP relational data model.

## Core Design Rules
- The state store is authoritative for structured state.
- Artifacts are stored externally but referenced from the state store.
- All primary IDs are stable opaque strings.
- Event ordering is determined by a monotonic sequence.

## Tables

### capabilities
- `capability_id` PK
- `name`
- `version`
- `image_ref`
- `model_ref`
- `resource_tier`
- `permission_profile`
- `queue_mode`
- `runtime_requirements_json`
- `created_at`
- `updated_at`

### runtimes
- `runtime_id` PK
- `hostname`
- `release_version`
- `status`
- `health_status`
- `last_seen_at`
- `last_heartbeat_at`
- `metadata_json`
- `created_at`
- `updated_at`

### agents
- `agent_id` PK
- `capability_id` FK -> capabilities
- `assigned_runtime_id` FK nullable -> runtimes
- `queue_id`
- `status`
- `workspace_ref`
- `last_seen_at`
- `created_at`
- `updated_at`

### agent_runtime_bindings
- `agent_id` FK -> agents
- `runtime_id` FK -> runtimes
- `binding_status`
- `bound_at`
- composite PK `(agent_id, runtime_id, bound_at)`

### messages
- `message_id` PK
- `target_type`
- `target_id`
- `text`
- `metadata_json`
- `created_at`

### jobs
- `job_id` PK
- `message_id` FK -> messages
- `target_agent_id` FK nullable -> agents
- `target_queue`
- `status`
- `retry_count`
- `max_retries`
- `latest_run_id` nullable
- `result_artifact_id` nullable
- `created_at`
- `updated_at`

### runs
- `run_id` PK
- `job_id` FK -> jobs
- `agent_id` FK -> agents
- `runtime_id` FK -> runtimes
- `attempt`
- `status`
- `started_at`
- `finished_at`
- `error_artifact_id` nullable
- `created_at`

### system_metadata
- `key` PK
- `value`
- `updated_at`

Used for persisted release/schema version state and rollback targets.

Unique constraint:
- `(job_id, attempt)`

### leases
- `lease_id` PK
- `run_id` FK -> runs
- `agent_id` FK -> agents
- `runtime_id` FK -> runtimes
- `fencing_token`
- `status`
- `expires_at`
- `created_at`
- `released_at` nullable

Unique constraints:
- one active lease per run
- one active fencing token per run

### artifacts
- `artifact_id` PK
- `job_id` FK nullable -> jobs
- `run_id` FK nullable -> runs
- `kind`
- `content_type`
- `storage_ref`
- `checksum`
- `size_bytes`
- `created_at`

### job_artifacts
- `job_id` FK -> jobs
- `artifact_id` FK -> artifacts
- `role`
- composite PK `(job_id, artifact_id, role)`

### run_artifacts
- `run_id` FK -> runs
- `artifact_id` FK -> artifacts
- `role`
- composite PK `(run_id, artifact_id, role)`

### handoffs
- `handoff_id` PK
- `source_job_id` FK -> jobs
- `created_at`

### handoff_artifacts
- `handoff_id` FK -> handoffs
- `artifact_id` FK -> artifacts
- composite PK `(handoff_id, artifact_id)`

### handoff_jobs
- `handoff_id` FK -> handoffs
- `job_id` FK -> jobs
- composite PK `(handoff_id, job_id)`

### events
- `event_id` PK
- `event_seq` unique bigint
- `job_id` FK nullable -> jobs
- `run_id` FK nullable -> runs
- `agent_id` FK nullable -> agents
- `runtime_id` FK nullable -> runtimes
- `event_type`
- `body_json`
- `created_at`

### event_job_links
- `event_id` FK -> events
- `job_id` FK -> jobs
- `relation`
- composite PK `(event_id, job_id, relation)`

### idempotency_keys
- `idempotency_key` PK component
- `endpoint`
- `request_hash`
- `response_json`
- `created_at`
- `expires_at`
- composite PK `(idempotency_key, endpoint)`

## Required Indexes
- `jobs(status, created_at)`
- `jobs(target_agent_id, status, created_at)`
- `runs(job_id, attempt)`
- `runs(runtime_id, status, created_at)`
- `leases(run_id, status)`
- `leases(runtime_id, status, expires_at)`
- `artifacts(job_id, created_at)`
- `artifacts(run_id, created_at)`
- `events(job_id, event_seq)`
- `events(run_id, event_seq)`
- `agents(status, created_at)`
- `runtimes(status, last_seen_at)`
- `idempotency_keys(expires_at)`

## Relationship Rules
- A job has one originating message.
- A job has one or more runs over time.
- A run belongs to exactly one job.
- A run belongs to exactly one agent and one runtime.
- A run may have many artifacts.
- A job may have many artifacts.
- A handoff may create many child jobs.

## Artifact Roles
Valid roles include:
- `prompt`
- `transcript_log`
- `exec_log`
- `result`
- `failure_evidence`

## Event Model Rules
- `event_seq` is assigned by the control plane.
- `event_seq` is the only authoritative ordering key.
- Timestamps are informational, not ordering truth.
- Multi-job events such as handoff provenance must use `event_job_links` in addition to the primary event row.

## Idempotency Rules
- Idempotent write endpoints must persist replay records in `idempotency_keys`.
- `request_hash` must represent the normalized request body for conflict detection.
- `response_json` stores the replayable response body.

## Mutation Rules
- Terminal job and run rows are immutable except for audit-safe metadata fields.
- Lease rows become immutable after `expired` or `released`.
- Artifact rows are immutable after creation.

## Consistency Requirements
- `jobs.latest_run_id` must reference the most recent run by attempt.
- `jobs.result_artifact_id` must reference an artifact with role `result`.
- Active lease uniqueness must be enforced transactionally.
- Event emission and state mutation must be committed atomically where possible.
