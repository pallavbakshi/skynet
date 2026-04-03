-- Add 'extraction_diagnostics' to the allowed roles for run_artifacts.
-- SQLite cannot ALTER CHECK constraints, so we recreate the table.

BEGIN;

CREATE TABLE run_artifacts_new (
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
  role TEXT NOT NULL,
  PRIMARY KEY (run_id, artifact_id, role),
  CHECK (role IN ('prompt', 'transcript_log', 'exec_log', 'result', 'failure_evidence', 'extraction_diagnostics'))
);

INSERT INTO run_artifacts_new SELECT * FROM run_artifacts;
DROP TABLE run_artifacts;
ALTER TABLE run_artifacts_new RENAME TO run_artifacts;

COMMIT;
