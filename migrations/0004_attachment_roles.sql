BEGIN;

ALTER TABLE artifacts DROP CONSTRAINT IF EXISTS chk_artifacts_kind;
ALTER TABLE job_artifacts DROP CONSTRAINT IF EXISTS chk_job_artifacts_role;

COMMIT;
