-- Dynamic Agent Mesh: self-registration, ephemeral agents, simplified routing.
-- See research/dynamic-agent-mesh-prd.md for design context.
--
-- This migration is non-destructive: existing agents are migrated in place.
-- The sweeper will clean up stale ones after the heartbeat grace period.

BEGIN;

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS output_contract_json JSONB;

-- ── Step 1: Add new columns BEFORE dropping old ones (enables data migration) ──

ALTER TABLE agents ADD COLUMN IF NOT EXISTS capabilities JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE runtimes ADD COLUMN IF NOT EXISTS agent_id TEXT;

-- ── Step 2: Migrate existing data (safe: skips if old columns already dropped) ──

-- Populate capabilities from capability FK
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agents' AND column_name = 'capability_id'
    ) THEN
        UPDATE agents SET capabilities = (
            SELECT json_build_array(c.name)
            FROM capabilities c
            WHERE c.capability_id = agents.capability_id
        ) WHERE agents.capability_id IS NOT NULL AND agents.capabilities = '[]'::jsonb;
    END IF;
END $$;

-- Initialize last_heartbeat_at from last_seen_at
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agents' AND column_name = 'last_seen_at'
    ) THEN
        UPDATE agents SET last_heartbeat_at = last_seen_at
        WHERE last_seen_at IS NOT NULL;
    END IF;
END $$;

-- Link runtimes to agents via assigned_runtime_id
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'agents' AND column_name = 'assigned_runtime_id'
    ) THEN
        UPDATE runtimes SET agent_id = a.agent_id
        FROM agents a WHERE runtimes.runtime_id = a.assigned_runtime_id
        AND runtimes.agent_id IS NULL;
    END IF;
END $$;

-- ── Step 3: Drop old columns and constraints ──

ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_capability_id_fkey;
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_assigned_runtime_id_fkey;
ALTER TABLE agents DROP CONSTRAINT IF EXISTS chk_agents_status;

ALTER TABLE agents DROP COLUMN IF EXISTS capability_id;
ALTER TABLE agents DROP COLUMN IF EXISTS assigned_runtime_id;
ALTER TABLE agents DROP COLUMN IF EXISTS last_seen_at;

-- ── Step 4: New constraints ──

ALTER TABLE agents ADD CONSTRAINT chk_agents_status
  CHECK (status IN ('idle', 'busy', 'draining'));

-- Runtime.agent_id: FK + UNIQUE for 1:1 binding
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'runtimes_agent_id_fkey'
    ) THEN
        ALTER TABLE runtimes ADD CONSTRAINT runtimes_agent_id_fkey
          FOREIGN KEY (agent_id) REFERENCES agents(agent_id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_runtimes_agent_id'
    ) THEN
        ALTER TABLE runtimes ADD CONSTRAINT uq_runtimes_agent_id UNIQUE (agent_id);
    END IF;
END $$;

-- ── Step 5: Runs, leases, events — drop FK, keep agent_id as audit history ──
-- agent_id is preserved after agent deletion (no ON DELETE SET NULL).

ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_agent_id_fkey;
ALTER TABLE runs ALTER COLUMN agent_id DROP NOT NULL;

ALTER TABLE leases DROP CONSTRAINT IF EXISTS leases_agent_id_fkey;
ALTER TABLE leases ALTER COLUMN agent_id DROP NOT NULL;

ALTER TABLE events DROP CONSTRAINT IF EXISTS events_agent_id_fkey;

-- Jobs: keep FK with ON DELETE SET NULL (jobs can be retargeted)
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_target_agent_id_fkey;
ALTER TABLE jobs ADD CONSTRAINT jobs_target_agent_id_fkey
  FOREIGN KEY (target_agent_id) REFERENCES agents(agent_id) ON DELETE SET NULL;

-- ── Step 6: Discovery indexes ──

CREATE INDEX IF NOT EXISTS ix_agents_status_heartbeat ON agents(status, last_heartbeat_at);
CREATE INDEX IF NOT EXISTS ix_agents_capabilities_gin ON agents USING GIN (capabilities);

-- ── Step 7: Drop removed tables ──

DROP TABLE IF EXISTS agent_runtime_bindings;
DROP TABLE IF EXISTS capability_pools;

-- No destructive DELETE — existing agents migrate in place.
-- Stale agents will be cleaned up by the sweeper after the heartbeat grace period.

COMMIT;
