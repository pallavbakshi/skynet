-- Add summary_json column to jobs table.

ALTER TABLE jobs ADD COLUMN summary_json JSON;
