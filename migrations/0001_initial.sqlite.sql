-- AGP initial schema (SQLite dialect)
-- Translated from 0001_initial.sql for SQLite compatibility.

BEGIN;

CREATE TABLE IF NOT EXISTS _sqlite_sequences (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO _sqlite_sequences (name, value) VALUES ('events_event_seq', 0);

CREATE TABLE capabilities (
  capability_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  image_ref TEXT NOT NULL,
  model_ref TEXT NOT NULL,
  resource_tier TEXT NOT NULL,
  permission_profile TEXT NOT NULL,
  queue_mode TEXT NOT NULL,
  runtime_requirements_json TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CHECK (resource_tier IN ('small', 'medium', 'large', 'gpu')),
  CHECK (queue_mode IN ('agent', 'capability_pool'))
);

CREATE UNIQUE INDEX idx_capabilities_name_version ON capabilities(name, version);

CREATE TABLE runtimes (
  runtime_id TEXT PRIMARY KEY,
  hostname TEXT NOT NULL,
  release_version TEXT NOT NULL,
  status TEXT NOT NULL,
  health_status TEXT NOT NULL,
  last_seen_at TIMESTAMP NOT NULL,
  last_heartbeat_at TIMESTAMP,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CHECK (status IN ('registering', 'idle', 'busy', 'degraded', 'offline', 'draining')),
  CHECK (health_status IN ('healthy', 'degraded', 'unreachable', 'draining'))
);

CREATE TABLE agents (
  agent_id TEXT PRIMARY KEY,
  capability_id TEXT NOT NULL REFERENCES capabilities(capability_id),
  assigned_runtime_id TEXT REFERENCES runtimes(runtime_id),
  queue_id TEXT NOT NULL,
  status TEXT NOT NULL,
  workspace_ref TEXT,
  last_seen_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CHECK (status IN ('provisioning', 'idle', 'busy', 'degraded', 'draining', 'terminated'))
);

CREATE UNIQUE INDEX idx_agents_queue_id ON agents(queue_id);

CREATE TABLE agent_runtime_bindings (
  agent_id TEXT NOT NULL REFERENCES agents(agent_id),
  runtime_id TEXT NOT NULL REFERENCES runtimes(runtime_id),
  binding_status TEXT NOT NULL,
  bound_at TIMESTAMP NOT NULL,
  PRIMARY KEY (agent_id, runtime_id, bound_at),
  CHECK (binding_status IN ('active', 'released', 'failed'))
);

CREATE TABLE messages (
  message_id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  text TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL,
  CHECK (target_type IN ('agent', 'capability'))
);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(message_id),
  target_agent_id TEXT REFERENCES agents(agent_id),
  target_queue TEXT NOT NULL,
  status TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  max_retries INTEGER NOT NULL DEFAULT 3,
  latest_run_id TEXT,
  result_artifact_id TEXT,
  output_contract_json TEXT,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CHECK (status IN ('accepted', 'queued', 'running', 'interrupt_requested', 'completed', 'failed', 'cancelled', 'blocked')),
  CHECK (retry_count >= 0),
  CHECK (max_retries >= 0),
  CHECK (target_queue <> '' AND (target_agent_id IS NULL OR target_agent_id <> '')),
  CHECK (target_queue LIKE 'agent:%' OR target_queue LIKE 'capability:%')
);

CREATE TABLE queue_deliveries (
  delivery_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  target_queue TEXT NOT NULL,
  state TEXT NOT NULL,
  delivery_attempt INTEGER NOT NULL DEFAULT 0,
  available_at TIMESTAMP NOT NULL,
  last_delivered_at TIMESTAMP,
  acked_at TIMESTAMP,
  dead_lettered_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL,
  CHECK (state IN ('pending', 'delivered', 'acked', 'dead_lettered')),
  CHECK (delivery_attempt >= 0)
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  agent_id TEXT NOT NULL REFERENCES agents(agent_id),
  runtime_id TEXT NOT NULL REFERENCES runtimes(runtime_id),
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL,
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  error_artifact_id TEXT,
  created_at TIMESTAMP NOT NULL,
  UNIQUE (job_id, attempt),
  CHECK (attempt > 0),
  CHECK (status IN ('created', 'leased', 'running', 'recovering', 'completed', 'failed', 'abandoned', 'cancelled'))
);

CREATE TABLE leases (
  lease_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  agent_id TEXT NOT NULL REFERENCES agents(agent_id),
  runtime_id TEXT NOT NULL REFERENCES runtimes(runtime_id),
  fencing_token INTEGER NOT NULL,
  status TEXT NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP NOT NULL,
  released_at TIMESTAMP,
  CHECK (fencing_token > 0),
  CHECK (status IN ('active', 'expired', 'released'))
);

CREATE TABLE artifacts (
  artifact_id TEXT PRIMARY KEY,
  job_id TEXT REFERENCES jobs(job_id),
  run_id TEXT REFERENCES runs(run_id),
  kind TEXT NOT NULL,
  content_type TEXT NOT NULL,
  storage_ref TEXT NOT NULL,
  checksum TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CHECK (kind IN ('prompt', 'transcript_log', 'exec_log', 'result', 'failure_evidence')),
  CHECK (size_bytes >= 0)
);

CREATE TABLE job_artifacts (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  role TEXT NOT NULL,
  PRIMARY KEY (job_id, artifact_id, role),
  CHECK (role IN ('prompt', 'transcript_log', 'exec_log', 'result', 'failure_evidence'))
);

CREATE TABLE run_artifacts (
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  role TEXT NOT NULL,
  PRIMARY KEY (run_id, artifact_id, role),
  CHECK (role IN ('prompt', 'transcript_log', 'exec_log', 'result', 'failure_evidence'))
);

CREATE TABLE handoffs (
  handoff_id TEXT PRIMARY KEY,
  source_job_id TEXT NOT NULL REFERENCES jobs(job_id),
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE handoff_artifacts (
  handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  PRIMARY KEY (handoff_id, artifact_id)
);

CREATE TABLE handoff_jobs (
  handoff_id TEXT NOT NULL REFERENCES handoffs(handoff_id),
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  PRIMARY KEY (handoff_id, job_id)
);

CREATE TABLE events (
  event_id TEXT PRIMARY KEY,
  event_seq INTEGER NOT NULL UNIQUE,
  job_id TEXT REFERENCES jobs(job_id),
  run_id TEXT REFERENCES runs(run_id),
  agent_id TEXT REFERENCES agents(agent_id),
  runtime_id TEXT REFERENCES runtimes(runtime_id),
  event_type TEXT NOT NULL,
  body_json TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE event_job_links (
  event_id TEXT NOT NULL REFERENCES events(event_id),
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  relation TEXT NOT NULL,
  PRIMARY KEY (event_id, job_id, relation),
  CHECK (relation IN ('primary', 'source', 'child', 'related'))
);

CREATE TABLE idempotency_keys (
  idempotency_key TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL,
  expires_at TIMESTAMP NOT NULL,
  PRIMARY KEY (idempotency_key, endpoint)
);

CREATE TABLE health_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  health_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE capability_pools (
  capability_id TEXT PRIMARY KEY REFERENCES capabilities(capability_id),
  queue_id TEXT NOT NULL UNIQUE,
  routing_policy TEXT NOT NULL DEFAULT 'least_recent',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE nudges (
  nudge_id TEXT PRIMARY KEY,
  target_agent_id TEXT NOT NULL,
  priority INTEGER NOT NULL,
  source TEXT NOT NULL,
  payload TEXT NOT NULL,
  job_id TEXT REFERENCES jobs(job_id),
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  delivered_at TIMESTAMP,
  CHECK (status IN ('pending', 'delivered', 'expired')),
  CHECK (source IN ('human', 'job_completion', 'agenda_setter', 'system'))
);

CREATE TABLE system_metadata (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TIMESTAMP NOT NULL
);

CREATE INDEX idx_jobs_status_created_at ON jobs(status, created_at);
CREATE INDEX idx_jobs_target_agent_status_created_at ON jobs(target_agent_id, status, created_at);
CREATE INDEX idx_jobs_target_queue_status_created_at ON jobs(target_queue, status, created_at);
CREATE INDEX idx_queue_deliveries_queue_state_available_at ON queue_deliveries(target_queue, state, available_at);
CREATE INDEX idx_queue_deliveries_job_id ON queue_deliveries(job_id);
CREATE INDEX idx_runs_job_attempt ON runs(job_id, attempt);
CREATE INDEX idx_runs_runtime_status_created_at ON runs(runtime_id, status, created_at);
CREATE INDEX idx_leases_run_status ON leases(run_id, status);
CREATE INDEX idx_leases_runtime_status_expires_at ON leases(runtime_id, status, expires_at);
CREATE INDEX idx_artifacts_job_created_at ON artifacts(job_id, created_at);
CREATE INDEX idx_artifacts_run_created_at ON artifacts(run_id, created_at);
CREATE INDEX idx_events_job_event_seq ON events(job_id, event_seq);
CREATE INDEX idx_events_run_event_seq ON events(run_id, event_seq);
CREATE INDEX idx_agents_status_created_at ON agents(status, created_at);
CREATE INDEX idx_runtimes_status_last_seen_at ON runtimes(status, last_seen_at);
CREATE INDEX idx_idempotency_keys_expires_at ON idempotency_keys(expires_at);
CREATE UNIQUE INDEX idx_leases_active_run ON leases(run_id) WHERE status = 'active';
CREATE UNIQUE INDEX idx_leases_active_run_fencing ON leases(run_id, fencing_token) WHERE status = 'active';
CREATE UNIQUE INDEX idx_runs_active_agent ON runs(agent_id) WHERE status IN ('leased', 'running', 'recovering');

COMMIT;
