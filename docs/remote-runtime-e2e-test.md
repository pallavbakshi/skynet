# Remote E2E Test Guide

Two tested topologies for cross-machine AGP end-to-end testing between a Mac
and an Ubuntu server (`user` / `skunkwork` at `your-server.example.com`).

| Topology | Control Plane | Runtime | Tunnel |
|----------|--------------|---------|--------|
| [A. Local CP, remote runtime](#a-local-cp-remote-runtime) | Mac | Ubuntu (user) | `ssh -R` (reverse) |
| [B. Remote CP, local runtime](#b-remote-cp-local-runtime) | Ubuntu (user) | Mac | `ssh -L` (forward) |

---

## Prerequisites

### Mac
- AGP installed: `uv run agp --help`
- SSH access: `ssh user@your-server.example.com`
- `codex` CLI >= 0.116.0 (`codex --version`)
- tmux installed
- `OPENROUTER_API_KEY` or `OPENAI_API_KEY` in shell env

### Ubuntu (user)
- AGP repo at `~/projects/skynet` with `uv run agp` working
- `codex` CLI >= 0.116.0 (install: `sudo npm install -g @openai/codex`)
- Do NOT use `ncodex` 0.0.0 — it has broken OpenRouter auth
- tmux installed
- `OPENAI_API_KEY` or `OPENROUTER_API_KEY` in shell env
- `~/.codex/config.toml` with model + project trust configured

See [openrouter-runtime-setup.md](openrouter-runtime-setup.md) for Codex
provider config if using OpenRouter.

---

## A. Local CP, remote runtime

Control plane on Mac, runtime executes on Ubuntu via tmux + codex.

### Architecture

```
┌─────────────────────┐      reverse SSH tunnel      ┌─────────────────────────┐
│  Mac (local)        │◄── ssh -R 7860:lo:7860 ─────│  Ubuntu (user)            │
│                     │                              │                         │
│  CP :7860           │  ◄── claim / heartbeat ───── │  agp runtime-work-loop  │
│  SQLite + sweepers  │  ◄── artifact upload ──────  │  tmux + codex           │
│  .agp-artifacts/    │                              │                         │
└─────────────────────┘                              └─────────────────────────┘
```

### Step-by-step

#### 1. Start local control plane

```bash
make local-reset && make local-initdb && make local-serve
# Verify: curl -s http://127.0.0.1:7860/health
```

#### 2. Open reverse SSH tunnel

In a separate terminal:

```bash
ssh -N -R 7860:127.0.0.1:7860 user@your-server.example.com
```

This makes `localhost:7860` on the remote forward back to your local CP.

Verify:

```bash
ssh user@your-server.example.com "curl -s http://127.0.0.1:7860/health"
# → {"ok":true,"data":{"status":"ok",...}}
```

#### 3. Seed + register + send

```bash
CP=http://127.0.0.1:7860

curl -s -X POST $CP/capabilities/seed \
  -H "Content-Type: application/json" \
  -d '{"capability_id":"cap_python","name":"Python Tester","version":"v1"}'

curl -s -X POST $CP/runtimes/register \
  -H "Content-Type: application/json" \
  -d '{"runtime_id":"rtm_sg","hostname":"skunkwork","metadata":{}}'

curl -s -X POST $CP/agents/up \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"agt_sg","capability_id":"cap_python","assigned_runtime_id":"rtm_sg"}'

curl -s -X POST $CP/messages/send \
  -H "Content-Type: application/json" \
  -d '{"target":{"type":"agent","id":"agt_sg"},"message":{"text":"What is the capital of France? Reply with just the city name.","metadata":{}}}'
# Note the job_id from the response
```

#### 4. Start runtime on remote

```bash
ssh user@your-server.example.com
cd ~/projects/skynet

AGP_ARTIFACT_BACKEND=http \
AGP_CODEX_TUI_MODE=true \
AGP_CODEX_CLI_COMMAND="codex -m gpt-5.4 -a never -s danger-full-access" \
AGP_CODEX_MAX_POLLS=240 \
AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
AGP_CODEX_IDLE_AFTER=5 \
uv run agp runtime-work-loop rtm_sg \
  --server-url http://127.0.0.1:7860 \
  --host-kind tmux \
  --adapter-kind codex \
  --agent-id agt_sg \
  --max-iterations 2 \
  --idle-sleep-seconds 2.0
```

#### 5. Verify (from Mac)

```bash
# Job status
curl -s http://127.0.0.1:7860/jobs/<JOB_ID> | python3 -m json.tool

# Result (artifacts stored locally via HTTP upload)
cat .agp-artifacts/rtm_sg/<JOB_ID>/result.txt

# Event trace
curl -s http://127.0.0.1:7860/jobs/<JOB_ID>/events | python3 -m json.tool

# Remote tmux pane
ssh user@your-server.example.com "tmux capture-pane -t agp-agt_sg -p | tail -20"
```

#### 6. Clean up

```bash
ssh user@your-server.example.com "tmux kill-session -t agp-agt_sg; pkill -f runtime-work-loop"
pkill -f "ssh -N -R 7860"
make local-reset
```

---

## B. Remote CP, local runtime

Control plane on Ubuntu, runtime executes on Mac via tmux + codex.

### Architecture

```
┌─────────────────────────┐      forward SSH tunnel     ┌─────────────────────┐
│  Ubuntu (user)            │                              │  Mac (local)        │
│                         │                              │                     │
│  CP :7860               │  ◄── claim / heartbeat ───── │  agp runtime-work-  │
│  SQLite + sweepers      │  ◄── artifact upload ──────  │  loop               │
│  .agp-artifacts/        │──── ssh -L 7860:lo:7860 ───►│  tmux + codex       │
└─────────────────────────┘                              └─────────────────────┘
```

### Step-by-step

#### 1. Start control plane on remote

```bash
ssh user@your-server.example.com
cd ~/projects/skynet

make local-reset && make local-initdb && make local-serve
# Verify: curl -s http://127.0.0.1:7860/health
```

Leave this SSH session open (CP runs in foreground).

#### 2. Open forward SSH tunnel (from Mac)

In a terminal on Mac:

```bash
ssh -N -L 7860:127.0.0.1:7860 user@your-server.example.com
```

This makes `localhost:7860` on your Mac forward to the remote CP.

Verify locally:

```bash
curl -s http://127.0.0.1:7860/health
# → {"ok":true,"data":{"status":"ok",...}}
```

#### 3. Seed + register + send (from Mac)

All curls hit `localhost:7860` which tunnels to the remote CP:

```bash
CP=http://127.0.0.1:7860

curl -s -X POST $CP/capabilities/seed \
  -H "Content-Type: application/json" \
  -d '{"capability_id":"cap_python","name":"Python Tester","version":"v1"}'

curl -s -X POST $CP/runtimes/register \
  -H "Content-Type: application/json" \
  -d '{"runtime_id":"rtm_mac","hostname":"'$(hostname)'","metadata":{}}'

curl -s -X POST $CP/agents/up \
  -H "Content-Type: application/json" \
  -d '{"agent_id":"agt_mac","capability_id":"cap_python","assigned_runtime_id":"rtm_mac"}'

curl -s -X POST $CP/messages/send \
  -H "Content-Type: application/json" \
  -d '{"target":{"type":"agent","id":"agt_mac"},"message":{"text":"What is the capital of France? Reply with just the city name.","metadata":{}}}'
# Note the job_id
```

#### 4. Start runtime on Mac

```bash
cd ~/projects/skynet

AGP_ARTIFACT_BACKEND=http \
AGP_CODEX_TUI_MODE=true \
AGP_CODEX_CLI_COMMAND="ncodex -m openai/gpt-5.3-codex -a never -s danger-full-access" \
AGP_CODEX_MAX_POLLS=240 \
AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 \
AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
AGP_CODEX_IDLE_AFTER=5 \
uv run agp runtime-work-loop rtm_mac \
  --server-url http://127.0.0.1:7860 \
  --host-kind tmux \
  --adapter-kind codex \
  --agent-id agt_mac \
  --max-iterations 2 \
  --idle-sleep-seconds 2.0
```

> On Mac, `ncodex` with OpenRouter works because `~/.config/codex/config.toml`
> has the `[model_providers.openrouter]` block configured. On the remote,
> use `codex` 0.116.0 with the native OpenAI key instead.

#### 5. Verify

```bash
# Job status (through the tunnel)
curl -s http://127.0.0.1:7860/jobs/<JOB_ID> | python3 -m json.tool

# Result — artifacts were uploaded via HTTP to the remote CP
# They are stored on the REMOTE disk, not locally
ssh user@your-server.example.com "cat ~/projects/skynet/.agp-artifacts/rtm_mac/<JOB_ID>/result.txt"

# Event trace
curl -s http://127.0.0.1:7860/jobs/<JOB_ID>/events | python3 -m json.tool

# Local tmux pane
tmux capture-pane -t agp-agt_mac -p | tail -20
```

#### 6. Clean up

```bash
# Local
tmux kill-session -t agp-agt_mac 2>/dev/null
pkill -f runtime-work-loop
pkill -f "ssh -N -L 7860"

# Remote
ssh user@your-server.example.com "cd ~/projects/skynet && make local-reset"
```

---

## Quick-repeat blocks

### Topology A: local CP, remote runtime

```bash
# T1: local CP
make local-reset && make local-initdb && make local-serve

# T2: tunnel
ssh -N -R 7860:127.0.0.1:7860 user@your-server.example.com

# T1: seed + send
CP=http://127.0.0.1:7860
curl -s -X POST $CP/capabilities/seed -H "Content-Type: application/json" \
  -d '{"capability_id":"cap_python","name":"Python Tester","version":"v1"}'
curl -s -X POST $CP/runtimes/register -H "Content-Type: application/json" \
  -d '{"runtime_id":"rtm_sg","hostname":"skunkwork","metadata":{}}'
curl -s -X POST $CP/agents/up -H "Content-Type: application/json" \
  -d '{"agent_id":"agt_sg","capability_id":"cap_python","assigned_runtime_id":"rtm_sg"}'
curl -s -X POST $CP/messages/send -H "Content-Type: application/json" \
  -d '{"target":{"type":"agent","id":"agt_sg"},"message":{"text":"What is 2+2? Just the number.","metadata":{}}}'

# T3: remote runtime
ssh user@your-server.example.com "cd ~/projects/skynet && \
  AGP_ARTIFACT_BACKEND=http AGP_CODEX_TUI_MODE=true \
  AGP_CODEX_CLI_COMMAND='codex -m gpt-5.4 -a never -s danger-full-access' \
  AGP_CODEX_MAX_POLLS=240 AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
  AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
  AGP_CODEX_IDLE_AFTER=5 \
  uv run agp runtime-work-loop rtm_sg \
    --server-url http://127.0.0.1:7860 \
    --host-kind tmux --adapter-kind codex \
    --agent-id agt_sg --max-iterations 2"

# Verify
cat .agp-artifacts/rtm_sg/*/result.txt
```

### Topology B: remote CP, local runtime

```bash
# T1: remote CP
ssh user@your-server.example.com "cd ~/projects/skynet && make local-reset && make local-initdb && make local-serve"
# (leave this running or use: ssh -t user@your-server.example.com "cd ~/projects/skynet && make local-serve")

# T2: tunnel
ssh -N -L 7860:127.0.0.1:7860 user@your-server.example.com

# T1 (local): seed + send
CP=http://127.0.0.1:7860
curl -s -X POST $CP/capabilities/seed -H "Content-Type: application/json" \
  -d '{"capability_id":"cap_python","name":"Python Tester","version":"v1"}'
curl -s -X POST $CP/runtimes/register -H "Content-Type: application/json" \
  -d '{"runtime_id":"rtm_mac","hostname":"'$(hostname)'","metadata":{}}'
curl -s -X POST $CP/agents/up -H "Content-Type: application/json" \
  -d '{"agent_id":"agt_mac","capability_id":"cap_python","assigned_runtime_id":"rtm_mac"}'
curl -s -X POST $CP/messages/send -H "Content-Type: application/json" \
  -d '{"target":{"type":"agent","id":"agt_mac"},"message":{"text":"What is 2+2? Just the number.","metadata":{}}}'

# T3 (local): runtime
cd ~/projects/skynet
AGP_ARTIFACT_BACKEND=http AGP_CODEX_TUI_MODE=true \
AGP_CODEX_CLI_COMMAND="ncodex -m openai/gpt-5.3-codex -a never -s danger-full-access" \
AGP_CODEX_MAX_POLLS=240 AGP_CODEX_POLL_INTERVAL_SECONDS=2.0 \
AGP_CODEX_IDLE_TIMEOUT_SECONDS=180.0 AGP_CODEX_IDLE_POLL_SECONDS=2.0 \
AGP_CODEX_IDLE_AFTER=5 \
uv run agp runtime-work-loop rtm_mac \
  --server-url http://127.0.0.1:7860 \
  --host-kind tmux --adapter-kind codex \
  --agent-id agt_mac --max-iterations 2

# Verify
ssh user@your-server.example.com "cat ~/projects/skynet/.agp-artifacts/rtm_mac/*/result.txt"
```

---

## Key differences between topologies

| | A (local CP) | B (remote CP) |
|---|---|---|
| CP runs on | Mac | Ubuntu (user) |
| Runtime runs on | Ubuntu (user) | Mac |
| SSH tunnel | `ssh -R` (reverse) | `ssh -L` (forward) |
| Artifacts stored on | Mac (local disk) | Ubuntu (remote disk) |
| Codex CLI | `codex` 0.116.0 (remote) | `ncodex` (local, OpenRouter) |
| Model | `gpt-5.4` (direct OpenAI) | `openai/gpt-5.3-codex` (OpenRouter) |
| Read result from | `cat .agp-artifacts/...` | `ssh user cat .agp-artifacts/...` |

---

## Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| Can't reach CP through tunnel | Tunnel not open or wrong direction | `-R` for remote runtime, `-L` for local runtime |
| `404` on `/runs/claim` | Agent not registered | Run `agents/up` curl before starting runtime |
| Codex 401 / WebSocket 404 | Old codex or missing config | Use `codex` 0.116.0+, check config.toml |
| `ncodex` version 0.0.0 | Dev build, broken auth | Use `codex` instead: `sudo npm install -g @openai/codex` |
| Port 7860 already in use | Leftover CP or other process | `lsof -i :7860` then kill, or `make local-reset` |
| Artifacts not found locally | Backend set to `localfs` | Must use `AGP_ARTIFACT_BACKEND=http` on the runtime side |
| Artifacts not found on remote | Backend set to `localfs` | Same — `http` backend uploads to CP over the tunnel |
| API key in transcript | Inline env in shell command | Known issue — treat transcripts as sensitive |

### Known issue: secrets in transcript

The codex adapter injects API keys inline in the tmux `send-keys` command,
so they appear in the `transcript_log` artifact:

```
OPENAI_API_KEY=sk-proj-... codex -m gpt-5.4 -a never ...
```

Until this is fixed upstream, treat transcript artifacts as sensitive.
