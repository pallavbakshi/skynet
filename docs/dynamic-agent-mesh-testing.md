# Dynamic Agent Mesh — Testing Guide

## Prerequisites

- Python 3.12+ with `uv`
- Docker Desktop (for Docker stack mode)
- `OPENROUTER_API_KEY` set in `.env` or environment
- `codex` (codex CLI) installed with an `openrouter` profile in `~/.codex/config.toml`

## Local Mode (SQLite, no infra)

### Start the control plane

```bash
make local-up
```

This does three things in one command:
1. Wipes any existing `agp.db`, logs, and artifacts
2. Runs `agp initdb` to create a fresh SQLite schema
3. Starts the CP server + lease sweeper + runtime sweeper as background processes

Verify:

```bash
make local-status
```

Expected output: 3 processes running, health OK, no agents registered.

### Start an agent runtime

```bash
make runtime
```

This starts a runtime that:
- Registers itself with the CP (`POST /runtimes/register`)
- Self-registers agent `agt_local` with capabilities `[code, python]` (`POST /agents/up`)
- Begins heartbeating and polling for work
- Uses codex with the OpenRouter profile for execution
- Launches codex in a tmux session (`agp-agt_local`)

Verify the agent appeared:

```bash
make local-status
```

Expected: `agt_local  code,python  idle` under Agents.

### Test agent discovery

```bash
# Should find agt_local
curl -s 'http://localhost:7860/agents?capability=code' | python3 -m json.tool

# Should return empty (exact match, no substring)
curl -s 'http://localhost:7860/agents?capability=rust' | python3 -m json.tool
```

### Send work

```bash
curl -s -X POST http://localhost:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H 'Idempotency-Key: test-001' \
    -d '{
      "target": {"type": "agent", "id": "agt_local"},
      "message": {"text": "what is 2+2? reply with just the number"}
    }' | python3 -m json.tool
```

Note the `job_id` from the response.

### Check job status

```bash
curl -s http://localhost:7860/jobs/<JOB_ID> | python3 -m json.tool
```

The job will progress through: `queued → running → completed`.

### Read the result

```bash
curl -s http://localhost:7860/artifacts/<RESULT_ARTIFACT_ID>/content | python3 -m json.tool
```

The `content` field contains the agent's answer.

### Watch codex execute (optional)

```bash
tmux attach -t agp-agt_local
```

Detach with `Ctrl-b d`.

### Stop everything

```bash
make local-down
```

This kills the CP, sweepers, and any running runtime processes.

### Verify binary presence

After stopping the runtime, the agent remains in the database for up to 60 seconds (heartbeat grace period). The sweeper deletes it once the grace expires. To verify:

```bash
# Immediately after runtime stops — agent still present
curl -s http://localhost:7860/agents | python3 -m json.tool

# After 60+ seconds — agent gone
curl -s http://localhost:7860/agents | python3 -m json.tool
```

## Docker Mode (Postgres, Redis, MinIO, Prometheus, Grafana)

### Start the full stack

```bash
make up
```

This builds Docker images and starts 8 containers:
- `agp-control-plane-1` — CP API server (port 7860)
- `agp-lease-sweeper-1` — lease expiry sweeper
- `agp-runtime-sweeper-1` — agent/runtime liveness sweeper
- `agp-postgres-1` — database
- `agp-redis-1` — queue transport
- `agp-minio-1` — artifact storage (ports 9000/9001)
- `agp-prometheus-1` — metrics (port 9090)
- `agp-grafana-1` — dashboards (port 3370)

Verify:

```bash
make status
```

### Connect a runtime

Runtimes run outside Docker, connecting to the CP via `localhost:7860`:

```bash
make runtime
```

Or manually:

```bash
OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
.venv/bin/python -m agp runtime-work-loop rtm-coder-1 \
    --agent-id coder-1 \
    --capabilities code,python \
    --server-url http://localhost:7860 \
    --host-kind tmux \
    --adapter-kind codex
```

### Send work and check results

Same as local mode — the CP API is on `localhost:7860` in both cases.

### Stop the Docker stack

```bash
make down
```

This stops all containers and removes volumes (clean slate).

## Makefile Reference

| Command | Description |
|---------|-------------|
| `make local-up` | Clean start: reset + init + serve (SQLite) |
| `make local-down` | Stop local CP + runtimes |
| `make local-status` | Show processes, health, and agents |
| `make runtime` | Start runtime with codex + OpenRouter |
| `make up` | Start full Docker stack |
| `make down` | Stop Docker stack + remove volumes |
| `make status` | Show Docker containers, processes, health, agents |
| `make stop` | Stop everything (local, Docker, kind) |
| `make stop-runtime` | Kill runtime worker processes |

## Idempotency Keys

The `/messages/send` endpoint supports idempotency keys via the `Idempotency-Key` header. If you resend with the same key, you get the cached response (same `job_id`). Use a unique key for each new request:

```bash
curl -s -X POST http://localhost:7860/messages/send \
    -H 'Idempotency-Key: unique-key-here' \
    ...
```

## Troubleshooting

### Port 7860 already in use

Check what's running:

```bash
lsof -tiTCP:7860 -sTCP:LISTEN
```

Common cause: old Docker containers from a previous project name. Kill them:

```bash
docker ps --format '{{.Names}}' | grep agp | xargs docker stop
docker ps -a --format '{{.Names}}' | grep agp | xargs docker rm
```

### Codex 401 Unauthorized

The codex CLI needs API credentials forwarded into the tmux session. Verify:

```bash
# Check tmux session has the env vars
tmux show-environment -t agp-agt_local | grep -E "OPENAI|OPENROUTER"
```

If using OpenRouter with `codex`, use the profile flag:

```bash
AGP_CODEX_CLI_COMMAND="codex -p openrouter -a never -s danger-full-access" make runtime
```

### Stale tmux session

If a runtime crashed, the tmux session may persist with stale env vars:

```bash
tmux kill-session -t agp-agt_local
make runtime
```

### Old database schema

If `GET /agents` returns fields like `capability_id` or `assigned_runtime_id`, the database has the old schema:

```bash
make local-down
rm -f agp.db agp.db-wal agp.db-shm
make local-up
```
