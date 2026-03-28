BEGIN;

CREATE TABLE artifacts_new (
  artifact_id TEXT PRIMARY KEY,
  job_id TEXT REFERENCES jobs(job_id),
  run_id TEXT REFERENCES runs(run_id),
  kind TEXT NOT NULL,
  content_type TEXT NOT NULL,
  storage_ref TEXT NOT NULL,
  checksum TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TIMESTAMP NOT NULL,
  CHECK (size_bytes >= 0)
);

INSERT INTO artifacts_new SELECT * FROM artifacts;
DROP TABLE artifacts;
ALTER TABLE artifacts_new RENAME TO artifacts;
CREATE INDEX IF NOT EXISTS idx_artifacts_job_created_at ON artifacts(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_created_at ON artifacts(run_id, created_at);

CREATE TABLE job_artifacts_new (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  role TEXT NOT NULL,
  PRIMARY KEY (job_id, artifact_id, role)
);

INSERT INTO job_artifacts_new SELECT * FROM job_artifacts;
DROP TABLE job_artifacts;
ALTER TABLE job_artifacts_new RENAME TO job_artifacts;

COMMIT;
