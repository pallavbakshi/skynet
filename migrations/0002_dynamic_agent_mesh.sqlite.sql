-- Dynamic Agent Mesh: self-registration, ephemeral agents, simplified routing.
-- SQLite dialect — recreates tables that need schema changes.
-- Migrates existing agent data from old schema (capability_id, assigned_runtime_id)
-- to new schema (capabilities JSONB array, metadata_json).

-- ── Drop removed tables ──

DROP TABLE IF EXISTS agent_runtime_bindings;
DROP TABLE IF EXISTS capability_pools;

-- ── Capture old agent-to-runtime linkage before we lose assigned_runtime_id ──
-- Old agents may have assigned_runtime_id. Grab that mapping now so we
-- can set agent_id on the new runtimes table later.
CREATE TABLE IF NOT EXISTS _agent_runtime_links (agent_id TEXT, runtime_id TEXT);
INSERT OR IGNORE INTO _agent_runtime_links (agent_id, runtime_id)
  SELECT agent_id, assigned_runtime_id FROM agents WHERE typeof(assigned_runtime_id) != 'null';

-- ── Recreate agents table with new schema ──

CREATE TABLE IF NOT EXISTS agents_new (
  agent_id TEXT PRIMARY KEY,
  capabilities TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  queue_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('idle', 'busy', 'draining')),
  workspace_ref TEXT,
  last_heartbeat_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
  created_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
  updated_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))
);

-- Migrate from old schema: map capability_id to capabilities array, last_seen_at to last_heartbeat_at
INSERT OR IGNORE INTO agents_new (agent_id, capabilities, metadata_json, queue_id, status, workspace_ref, last_heartbeat_at, created_at, updated_at)
  SELECT
    a.agent_id,
    CASE WHEN c.name IS NOT NULL THEN json_array(c.name) ELSE '[]' END,
    '{}',
    a.queue_id,
    CASE WHEN a.status IN ('idle', 'busy', 'draining') THEN a.status ELSE 'idle' END,
    a.workspace_ref,
    COALESCE(a.last_seen_at, datetime('now')),
    a.created_at,
    a.updated_at
  FROM agents a
  LEFT JOIN capabilities c ON c.capability_id = a.capability_id;

DROP TABLE IF EXISTS agents;
ALTER TABLE agents_new RENAME TO agents;

CREATE INDEX IF NOT EXISTS ix_agents_status_created ON agents(status, created_at);
CREATE INDEX IF NOT EXISTS ix_agents_status_heartbeat ON agents(status, last_heartbeat_at);

-- ── Recreate runs table — agent_id is plain text (no FK), preserves audit history ──

CREATE TABLE IF NOT EXISTS runs_new (
  run_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  agent_id TEXT,
  runtime_id TEXT NOT NULL REFERENCES runtimes(runtime_id),
  attempt INTEGER NOT NULL CHECK (attempt > 0),
  status TEXT NOT NULL CHECK (status IN ('created','leased','running','recovering','completed','failed','abandoned','cancelled')),
  started_at TIMESTAMP,
  finished_at TIMESTAMP,
  error_artifact_id TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO runs_new SELECT * FROM runs;
DROP TABLE IF EXISTS runs;
ALTER TABLE runs_new RENAME TO runs;
CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_job_attempt ON runs(job_id, attempt);
CREATE INDEX IF NOT EXISTS ix_runs_job_attempt ON runs(job_id, attempt);
CREATE INDEX IF NOT EXISTS ix_runs_runtime_status_created ON runs(runtime_id, status, created_at);

-- ── Recreate leases table — agent_id is plain text (no FK), preserves audit history ──

CREATE TABLE IF NOT EXISTS leases_new (
  lease_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  agent_id TEXT,
  runtime_id TEXT NOT NULL REFERENCES runtimes(runtime_id),
  fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
  status TEXT NOT NULL CHECK (status IN ('active','expired','released')),
  expires_at TIMESTAMP NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
  released_at TIMESTAMP
);

INSERT OR IGNORE INTO leases_new SELECT * FROM leases;
DROP TABLE IF EXISTS leases;
ALTER TABLE leases_new RENAME TO leases;
CREATE INDEX IF NOT EXISTS ix_leases_run_status ON leases(run_id, status);
CREATE INDEX IF NOT EXISTS ix_leases_runtime_status_expires ON leases(runtime_id, status, expires_at);

-- ── Recreate runtimes with agent_id UNIQUE for 1:1 binding ──

CREATE TABLE IF NOT EXISTS runtimes_new (
  runtime_id TEXT PRIMARY KEY,
  agent_id TEXT UNIQUE,
  hostname TEXT NOT NULL,
  release_version TEXT NOT NULL,
  status TEXT NOT NULL,
  health_status TEXT NOT NULL,
  last_seen_at TIMESTAMP NOT NULL,
  last_heartbeat_at TIMESTAMP,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
  updated_at TIMESTAMP NOT NULL DEFAULT (datetime('now')),
  CHECK (status IN ('registering', 'idle', 'busy', 'degraded', 'offline', 'draining')),
  CHECK (health_status IN ('healthy', 'degraded', 'unreachable', 'draining'))
);

INSERT OR IGNORE INTO runtimes_new (runtime_id, agent_id, hostname, release_version, status, health_status, last_seen_at, last_heartbeat_at, metadata_json, created_at, updated_at)
  SELECT r.runtime_id, l.agent_id, r.hostname, r.release_version, r.status, r.health_status, r.last_seen_at, r.last_heartbeat_at, r.metadata_json, r.created_at, r.updated_at
  FROM runtimes r
  LEFT JOIN _agent_runtime_links l ON l.runtime_id = r.runtime_id;
DROP TABLE IF EXISTS runtimes;
ALTER TABLE runtimes_new RENAME TO runtimes;
CREATE INDEX IF NOT EXISTS ix_runtimes_status_lastseen ON runtimes(status, last_seen_at);

DROP TABLE IF EXISTS _agent_runtime_links;
