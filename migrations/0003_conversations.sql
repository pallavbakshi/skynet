BEGIN;

ALTER TABLE messages ADD COLUMN conversation_id TEXT;
ALTER TABLE messages ADD COLUMN reply_to_message_id TEXT;
ALTER TABLE jobs ADD COLUMN conversation_id TEXT;

CREATE INDEX idx_messages_conversation_created_at ON messages(conversation_id, created_at);
CREATE INDEX idx_jobs_conversation_id ON jobs(conversation_id);

COMMIT;
