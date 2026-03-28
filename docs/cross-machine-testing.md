# Cross-Machine Testing Guide

How to run the AGP control plane and runtime agent on separate machines and verify end-to-end communication. Covers all topology combinations tested as of 2026-03-28.

## Infrastructure

| Name | Role | Address |
|------|------|---------|
| `user` | Remote server (Ubuntu) | `your-server.example.com` |
| Mac | Local development machine | — |

## Prerequisites

Both machines must have:
- The `skynet` repo checked out and up to date
- `uv` installed
- `codex` CLI installed with the `openrouter` profile in `~/.codex/config.toml`
- `OPENROUTER_API_KEY` in the environment (or stored via `servu keys user set OPENROUTER_API_KEY <key>`)

### `~/.codex/config.toml` (required on any machine running the runtime)

```toml
model = "gpt-5.4"
model_provider = "openai"
model_reasoning_effort = "medium"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"

[profiles.openrouter]
model_provider = "openrouter"
model_reasoning_effort = "low"

[projects."/path/to/your/project"]
trust_level = "trusted"

[notice]
hide_full_access_warning = true
hide_rate_limit_model_nudge = true
```

---

## Scenario 1 — Remote CP (no Docker) + Local Runtime

CP runs on `user` using SQLite. Runtime + codex run on Mac.

### Start CP on user

```bash
ssh user@your-server.example.com
cd /home/user/projects/skynet
make local-up
```

Verify from Mac:
```bash
curl -s http://your-server.example.com:7860/ops/health
```

### Start runtime on Mac

```bash
# Default AGP_REMOTE_SERVER_URL=http://your-server.example.com:7860 is already set in Makefile
make runtime-remote
```

### Verify and test

```bash
# Agent should appear on remote CP
curl -s http://your-server.example.com:7860/agents

# Send a job
curl -s -X POST http://your-server.example.com:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: test-$(date +%s)" \
    -d '{"target": {"type": "agent", "id": "agt_local"}, "message": {"text": "what is 2+2? reply with just the number"}}'
```

### Teardown

```bash
# Mac
make stop-runtime

# user
cd /home/user/projects/skynet && make local-down
```

---

## Scenario 2 — Remote CP (Docker) + Local Runtime

CP runs on `user` inside Docker (Postgres + Redis + MinIO). Runtime + codex run on Mac.

### Start CP on user

```bash
ssh user@your-server.example.com
cd /home/user/projects/skynet
make up          # builds images, starts full stack
```

Verify from Mac:
```bash
curl -s http://your-server.example.com:7860/ops/health
```

### Start runtime on Mac

```bash
make runtime-remote
```

### Verify and test

```bash
# Agent should appear on remote CP
curl -s http://your-server.example.com:7860/agents

# Send a job
curl -s -X POST http://your-server.example.com:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: test-$(date +%s)" \
    -d '{"target": {"type": "agent", "id": "agt_local"}, "message": {"text": "what is 2+2? reply with just the number"}}'
```

### Teardown

```bash
# Mac
make stop-runtime

# user
cd /home/user/projects/skynet && make down
```

---

## Scenario 3 — Local CP + Remote Runtime

CP runs on Mac (SQLite). Runtime + codex run on `user`.

The Mac is behind NAT so `user` can't reach it directly. An SSH **reverse tunnel** is used to forward the Mac's CP port through the SSH connection.

### Start CP on Mac

```bash
make local-up
```

### Open reverse tunnel

In a separate terminal (keep it open for the duration of the test):

```bash
ssh -N -R 7860:localhost:7860 -p 22 user@your-server.example.com
```

This makes port 7860 on `user` forward back to localhost:7860 on the Mac.

Verify the tunnel works — ask `user` to curl its own localhost:

```bash
ssh -p 22 user@your-server.example.com "curl -s http://localhost:7860/ops/health"
```

### Start runtime on user

```bash
OPENROUTER_API_KEY=$(servu keys user get OPENROUTER_API_KEY)

ssh -p 22 user@your-server.example.com bash << ENDSSH
cd /home/user/projects/skynet
export OPENROUTER_API_KEY="$OPENROUTER_API_KEY"
nohup make runtime > /tmp/agp-runtime-user.log 2>&1 &
echo PID:\$!
ENDSSH
```

The `make runtime` default server URL is `http://127.0.0.1:7860` which — via the reverse tunnel — resolves back to the Mac's local CP.

### Verify and test

```bash
# Agent from user should appear on local CP
curl -s http://localhost:7860/agents

# Send a job from Mac to agent running on user
curl -s -X POST http://localhost:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: test-$(date +%s)" \
    -d '{"target": {"type": "agent", "id": "agt_local"}, "message": {"text": "what is 2+2? reply with just the number"}}'
```

### Teardown

```bash
# Kill tunnel (Ctrl-C the ssh -N process)

# Stop runtime on user
ssh -p 22 user@your-server.example.com bash << 'ENDSSH'
pkill -f "agp runtime" 2>/dev/null || true
tmux kill-session -t agp-agt_local 2>/dev/null || true
ENDSSH

# Stop local CP
make local-down
```

---

## Scenario 4 — user Docker CP + user Process Runtime

Both CP and runtime on `user`. CP in Docker (Postgres/Redis/MinIO), runtime as a plain process. Useful to confirm Docker CP works with a local runtime before going fully containerised.

### Start Docker CP on user

```bash
ssh user@your-server.example.com
cd /home/user/projects/skynet
make up
```

Verify from Mac:
```bash
curl -s http://your-server.example.com:7860/ops/health
```

### Start runtime process on user

```bash
OPENROUTER_API_KEY=$(servu keys user get OPENROUTER_API_KEY)

ssh -p 22 user@your-server.example.com bash << ENDSSH
cd /home/user/projects/skynet
export OPENROUTER_API_KEY="$OPENROUTER_API_KEY"
nohup make runtime > /tmp/agp-runtime-user.log 2>&1 &
echo PID:\$!
ENDSSH
```

The default `AGP_SERVER_URL=http://127.0.0.1:7860` in `make runtime` connects to the Docker CP on user's loopback.

### Verify and test

```bash
curl -s http://your-server.example.com:7860/agents

curl -s -X POST http://your-server.example.com:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: test-$(date +%s)" \
    -d '{"target": {"type": "agent", "id": "agt_local"}, "message": {"text": "what is 2+2? reply with just the number"}}'
```

### Teardown

```bash
ssh -p 22 user@your-server.example.com bash << 'ENDSSH'
pkill -f "agp runtime" 2>/dev/null || true
tmux kill-session -t agp-agt_local 2>/dev/null || true
ENDSSH

ssh user@your-server.example.com "cd /home/user/projects/skynet && make down"
```

---

## Scenario 5 — Mac Docker CP + user Process Runtime

Mac runs CP in Docker (Postgres/Redis/MinIO). user runs the runtime as a process. Mac is behind NAT so an SSH reverse tunnel is used.

### Start Docker CP on Mac

```bash
make up
```

### Open reverse tunnel

```bash
ssh -N -R 7860:localhost:7860 -p 22 user@your-server.example.com
```

Verify:
```bash
ssh -p 22 user@your-server.example.com "curl -s http://localhost:7860/ops/health"
```

### Start runtime process on user

```bash
OPENROUTER_API_KEY=$(servu keys user get OPENROUTER_API_KEY)

ssh -p 22 user@your-server.example.com bash << ENDSSH
cd /home/user/projects/skynet
export OPENROUTER_API_KEY="$OPENROUTER_API_KEY"
nohup make runtime > /tmp/agp-runtime-user.log 2>&1 &
echo PID:\$!
ENDSSH
```

### Verify and test

```bash
curl -s http://localhost:7860/agents

curl -s -X POST http://localhost:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: test-$(date +%s)" \
    -d '{"target": {"type": "agent", "id": "agt_local"}, "message": {"text": "what is 2+2? reply with just the number"}}'
```

### Teardown

```bash
# Kill tunnel (Ctrl-C the ssh -N process)

ssh -p 22 user@your-server.example.com bash << 'ENDSSH'
pkill -f "agp runtime" 2>/dev/null || true
tmux kill-session -t agp-agt_local 2>/dev/null || true
ENDSSH

make down
```

---

## Scenario 6 — user Docker CP + user Docker Runtime (Fully Containerised)

Both CP and runtime run as Docker containers on `user`. The runtime image (`agp-runtime`) connects to the CP via Docker's internal network. Uses MinIO for artifacts (same S3 backend as compose stack).

### Prerequisites

A credentials volume with codex config must be initialised once:

```bash
ssh -p 22 user@your-server.example.com bash << 'ENDSSH'
# Create the volume and seed with required files
docker volume create agp-credentials-test

docker run --rm -v agp-credentials-test:/credentials alpine sh -c '
  mkdir -p /credentials/codex
  touch /credentials/.volume-initialized
  cat > /credentials/codex/config.toml << EOF
model = "gpt-5.4"
model_provider = "openai"
model_reasoning_effort = "medium"

[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENROUTER_API_KEY"
wire_api = "responses"

[profiles.openrouter]
model = "openai/gpt-4o"
model_provider = "openrouter"
model_reasoning_effort = "low"

[projects."/workspace/main"]
trust_level = "trusted"

[projects."/app"]
trust_level = "trusted"

[notice]
hide_full_access_warning = true
hide_rate_limit_model_nudge = true
EOF
  chown -R 1000:1000 /credentials
'
ENDSSH
```

### Start Docker CP on user

```bash
ssh user@your-server.example.com "cd /home/user/projects/skynet && make up"
```

Verify from Mac:
```bash
curl -s http://your-server.example.com:7860/ops/health
```

### Start runtime container on user

Build the runtime image first if not already built:

```bash
ssh user@your-server.example.com "cd /home/user/projects/skynet && docker build --target agp-runtime -t agp-runtime ."
```

Start the container (note `agp_default` network and `agp-control-plane-1` hostname — set by `make up`):

```bash
OPENROUTER_KEY=$(servu keys user get OPENROUTER_API_KEY)

ssh -p 22 user@your-server.example.com "docker run -d --name agp-runtime-test \
  --network agp_default \
  -e AGP_RUNTIME_ID=rt_docker_01 \
  -e AGP_SERVER_URL=http://agp-control-plane-1:7860 \
  -e AGP_RUNTIME_AGENT_ID=agt_docker \
  -e AGP_RUNTIME_CAPABILITIES=code,python \
  -e AGP_LOG_ROOT=/tmp/logs \
  -e AGP_ARTIFACT_BACKEND=s3 \
  -e AGP_S3_ENDPOINT_URL=http://agp-minio-1:9000 \
  -e AGP_S3_ACCESS_KEY_ID=minioadmin \
  -e AGP_S3_SECRET_ACCESS_KEY=minioadmin \
  -e AGP_S3_BUCKET=agp-artifacts \
  -e AGP_S3_REGION=us-east-1 \
  -e AGP_S3_FORCE_PATH_STYLE=true \
  -e OPENROUTER_API_KEY=$OPENROUTER_KEY \
  -e CODEX_PROFILE=openrouter \
  -v agp-credentials-test:/credentials \
  agp-runtime"
```

Key points:
- `--network agp_default` — joins the compose network so CP is reachable by service name
- `AGP_S3_*` — must match compose.yaml MinIO config; runtime and CP share the same bucket
- `CODEX_PROFILE=openrouter` — the entrypoint injects `-p openrouter` into `AGP_CODEX_CLI_COMMAND`
- `AGP_LOG_ROOT=/tmp/logs` — overrides default `/logs` which requires the compose volume mount
- `AGP_OUTPUT_CHECKPOINT_DIR` defaults to `/tmp/agp-checkpoints` (set in Dockerfile ENV)

### Verify and test

```bash
# Agent should appear registered on remote CP
curl -s http://your-server.example.com:7860/agents

# Send a job
curl -s -X POST http://your-server.example.com:7860/messages/send \
    -H 'Content-Type: application/json' \
    -H "Idempotency-Key: test-$(date +%s)" \
    -d '{"target": {"type": "agent", "id": "agt_docker"}, "message": {"text": "what is 2+2? reply with just the number"}}'
```

### Teardown

```bash
ssh -p 22 user@your-server.example.com "docker rm -f agp-runtime-test; cd /home/user/projects/skynet && make down"
```

---

## Polling for job completion

All scenarios use the same pattern to wait for a job to complete:

```bash
JOB_ID="job_xxxxxxxxxxxx"
CP_URL="http://localhost:7860"   # or http://your-server.example.com:7860

for i in $(seq 1 18); do
    sleep 5
    STATUS=$(curl -s "$CP_URL/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['status'])")
    echo "  [$((i*5))s] $STATUS"
    if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ]; then break; fi
done

# Fetch result artifact
ART_ID=$(curl -s "$CP_URL/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['result_artifact_id'])")
curl -s "$CP_URL/artifacts/$ART_ID/content" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['content'])"
```

---

## Summary

| Scenario | CP location | CP mode | Runtime location | Runtime mode | Connectivity |
|----------|-------------|---------|-----------------|--------------|--------------|
| 1 | `user` | SQLite (process) | Mac | process | Direct — user port 7860 is public |
| 2 | `user` | Docker (Postgres/Redis/MinIO) | Mac | process | Direct — user port 7860 is public |
| 3 | Mac | SQLite (process) | `user` | process | SSH reverse tunnel: `ssh -N -R 7860:localhost:7860 user@your-server.example.com` |
| 4 | `user` | Docker (Postgres/Redis/MinIO) | `user` | process | Loopback (same machine) |
| 5 | Mac | Docker (Postgres/Redis/MinIO) | `user` | process | SSH reverse tunnel: `ssh -N -R 7860:localhost:7860 user@your-server.example.com` |
| 6 | `user` | Docker (Postgres/Redis/MinIO) | `user` | Docker container | Internal Docker network (`agp_default`) |

All six scenarios confirmed working on 2026-03-28 with `codex -p openrouter` and `OPENROUTER_API_KEY` returning answer `4` to "what is 2+2?".

## Key Makefile variables

| Variable | Default | Override example |
|----------|---------|-----------------|
| `AGP_REMOTE_SERVER_URL` | `http://your-server.example.com:7860` | `make runtime-remote AGP_REMOTE_SERVER_URL=http://other:7860` |
| `CODEX_PROFILE` | `openrouter` | `make runtime CODEX_PROFILE=openai` |
| `AGP_RUNTIME_AGENT_ID` | `agt_local` | `make runtime AGP_RUNTIME_AGENT_ID=my-agent` |
| `AGP_RUNTIME_CAPS` | `code,python` | `make runtime AGP_RUNTIME_CAPS=code,rust,python` |

## Docker runtime container notes

When running `agp-runtime` standalone (not via compose), override these defaults:

| Env var | Compose default | Standalone override |
|---------|-----------------|---------------------|
| `AGP_LOG_ROOT` | `/logs` (volume mount) | `/tmp/logs` |
| `AGP_RUNTIME_ARTIFACT_ROOT` | `/artifacts` (volume mount) | `/tmp/artifacts` |
| `AGP_OUTPUT_CHECKPOINT_DIR` | `/tmp/agp-checkpoints` (Dockerfile default) | — |
| `AGP_ARTIFACT_BACKEND` | `localfs` | `s3` (when CP uses compose MinIO) |
| `AGP_S3_ENDPOINT_URL` | — | `http://agp-minio-1:9000` |
| `CODEX_PROFILE` | — | `openrouter` (entrypoint injects `-p` flag) |

The runtime container must join the compose network (`--network agp_default`) to reach the CP by service name.
