-- Add summary_json column to jobs table.

BEGIN;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS summary_json JSON;

COMMIT;
