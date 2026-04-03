-- Add 'extraction_diagnostics' to the allowed roles for run_artifacts.

BEGIN;

ALTER TABLE run_artifacts DROP CONSTRAINT IF EXISTS chk_run_artifacts_role;
ALTER TABLE run_artifacts ADD CONSTRAINT chk_run_artifacts_role
  CHECK (role IN ('prompt', 'transcript_log', 'exec_log', 'result', 'failure_evidence', 'extraction_diagnostics'));

COMMIT;
