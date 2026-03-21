# Product Requirements Document

## Document
SkyOps Operator CLI PRD

## Version
0.1 Draft

## Purpose
Define a standalone operator CLI (`skyops`) that manages the full AGP infrastructure lifecycle — from installing dependencies through starting services, monitoring health, dispatching work, debugging plugins, and running disaster recovery — across both bare-metal and Docker deployment modes.

This PRD exists to answer:
- what commands an operator needs to go from a fresh server to a running AGP stack
- how skyops manages both Docker and bare-metal deployments through a single interface
- how the 48 operator/debug commands currently in `agp` CLI migrate to skyops
- how skyops integrates with the AGP Client SDK (`agp.client`) for API operations

## Why This Exists

Today, an operator setting up AGP must:
1. Manually install PostgreSQL, Redis, MinIO (or run `install_infra_tools.sh`)
2. Run `docker compose up` or start 5+ processes manually
3. Run `scripts/bootstrap_local_stack.py` to seed data
4. Use `agp send --server-url ... --operator-token ...` for every API call
5. Run separate Python scripts for backup, restore, failure drills, and validation
6. Use different tools for Docker vs bare-metal deployments

There are 12+ scripts in `scripts/` that each handle one piece of the operator workflow. There is no single tool that ties them together. The `agp` CLI mixes service entrypoints (5 commands) with 48 operator commands, making it unclear what is a production service vs. a human debugging tool.

## Goal

Provide a single `skyops` CLI binary that an operator uses for everything:
- first-time environment setup and dependency installation
- service lifecycle management (start/stop/restart/status)
- data seeding and database management
- work dispatch and job inspection (via `agp.client` SDK)
- monitoring, observability, and alerting
- backup, restore, and disaster recovery
- security and credential management
- plugin debugging and failure drills
- both Docker and bare-metal deployment modes through a unified interface

## Non-Goals

- Replacing the `agp` service binary (that stays as the lean process entrypoint)
- Building a web UI for skyops
- Multi-cluster or multi-region management
- Windows support (Linux and macOS only)
- Replacing k8s operators or Helm charts for production deployments

## Architecture

### Relationship to Other Components

```
┌────────────────────────────────────────────────────────┐
│  skyops  (operator CLI)                                │
│                                                        │
│  Uses:                                                 │
│    agp.client.AgpClient  — for API operations          │
│    agp.client.AgpProfile — for connection context      │
│    subprocess            — for process management      │
│    docker compose        — for Docker mode             │
│                                                        │
│  Manages:                                              │
│    skyops.toml           — operator configuration      │
│    ~/.agp/profiles/      — connection profiles         │
│    systemd units / PIDs  — bare-metal processes        │
│    compose.phase3.yaml   — Docker stack                │
└────────────────────────────────────────────────────────┘
         │                            │
         │ generates                  │ uses
         ▼                            ▼
┌──────────────────┐      ┌────────────────────────┐
│ ~/.agp/profiles/  │      │  agp  (service CLI)    │
│   default.toml   │      │                        │
│                  │      │  serve, initdb,        │
│  server_url      │      │  runtime-work-loop,    │
│  token           │      │  sweep-loop,           │
└──────────────────┘      │  sweep-runtimes-loop   │
         │                └────────────────────────┘
         │
         ▼
┌────────────────────────┐
│  orc (orchestrator)    │
│                        │
│  from agp.client       │
│    import AgpClient    │
│                        │
│  Reads same profiles   │
└────────────────────────┘
```

### Package Structure

```
src/
  skyops/
    __init__.py
    cli.py              # Typer root app, sub-apps
    config.py           # skyops.toml loading, SkyopsConfig model
    _infra.py           # deps check, deps install
    _lifecycle.py       # up, down, restart, status, ps
    _db.py              # db init, db seed, db status, db migrate
    _dispatch.py        # send, watch, jobs, agents, interrupt, fetch (thin wrappers over AgpClient)
    _monitor.py         # health, logs, metrics, alerts
    _backup.py          # backup create/restore/list/validate
    _security.py        # secrets show/generate/rotate
    _upgrade.py         # upgrade status/apply/rollback
    _plugins.py         # host/adapter/plugin debug sub-apps (migrated from agp cli.py)
    _drill.py           # failure injection drills
```

`pyproject.toml` adds: `skyops = "skyops.cli:app"`

### Configuration: `skyops.toml`

Skyops uses a TOML config file that lives at the project root (or `~/.skyops/config.toml` for global settings). Inspired by agentchattr's `config.toml` + `config.local.toml` layering pattern.

```toml
# skyops.toml
[stack]
mode = "docker"                           # "docker" or "bare-metal"
compose_file = "compose.phase3.yaml"      # path to compose file (docker mode)
project_name = "agp"                      # compose project name

[server]
host = "0.0.0.0"
port = 7860

[database]
url = "postgresql+psycopg://agp:agp@localhost:5432/agp"

[redis]
url = "redis://localhost:6379/0"

[s3]
endpoint_url = "http://localhost:9000"
access_key_id = "minioadmin"
secret_access_key = "minioadmin"
bucket = "agp-artifacts"

[security]
operator_token = ""
runtime_token = ""

[agents]
# Agents to bootstrap on first startup
[agents.agt_local]
capability_id = "cap_python"

[capabilities]
# Capabilities to seed on first startup
[capabilities.cap_python]
name = "Python Codex"
image_ref = "codex"
model_ref = "codex"

[runtime]
host_kind = "tmux"
adapter_kind = "codex"

[monitoring]
prometheus = true
grafana = true
```

**Override file:** `skyops.local.toml` (gitignored) overrides any section for local credentials and environment-specific values.

**Loading:** Merge `skyops.toml` + `skyops.local.toml` (local wins). Missing file is not an error for `skyops.local.toml`.

## Commands

### Infrastructure Setup

| Command | What It Does |
|---|---|
| `skyops init` | Interactive first-time setup: detects OS, checks deps, generates `skyops.toml` with sensible defaults, creates `~/.agp/profiles/default.toml` |
| `skyops deps check` | Reports which deps are installed and reachable (postgres, redis, minio, docker, kind, kubectl) |
| `skyops deps install` | Installs missing deps. Docker mode: pulls required images. Bare-metal: installs via apt/brew. Wraps existing `install_infra_tools*.sh` logic. |
| `skyops config show` | Prints current merged config with secrets masked |
| `skyops config set KEY VALUE` | Updates a value in `skyops.local.toml` (never modifies base `skyops.toml`) |

### Database & Seeding

| Command | What It Does |
|---|---|
| `skyops db init` | Creates schema. Docker: runs `agp initdb` inside the container. Bare-metal: runs `agp initdb` with env from config. |
| `skyops db seed` | Seeds capabilities and agents from `[capabilities]` and `[agents]` sections of `skyops.toml`. Calls `agp add-capability` then `AgpClient.send` for agent-up. Replaces `bootstrap_local_stack.py`. |
| `skyops db status` | Shows schema version, table counts, DB connection health, queue depth |
| `skyops db migrate` | Runs pending migrations (future — placeholder for now) |

### Service Lifecycle

| Command | What It Does |
|---|---|
| `skyops up` | Starts the full stack. Docker mode: `docker compose up -d --build`. Bare-metal: starts postgres/redis/minio if managed, then `agp serve`, `agp sweep-loop`, `agp sweep-runtimes-loop` as background processes. Runs `db init` + `db seed` on first boot. Generates `~/.agp/profiles/default.toml`. |
| `skyops up <service>` | Starts one service: `skyops up control-plane`, `skyops up redis`, `skyops up runtime` |
| `skyops down` | Stops all services. Docker: `docker compose down`. Bare-metal: sends SIGTERM to managed processes. |
| `skyops down <service>` | Stops one service |
| `skyops restart [service]` | Down then up (optionally for one service) |
| `skyops status` | Shows every service: running/stopped, PID/container, port, uptime, health. Uses port-probing (no PID files) inspired by agentchattr pattern. |
| `skyops ps` | Detailed process list with resource usage |

**Service detection (bare-metal):** Port probing via `lsof`/`ss`, same pattern as agentchattr's `is_server_running()`. No PID files.

**Docker mode lifecycle:**
```
skyops up (docker)
  ├── docker compose -f compose_file up -d --build
  ├── Wait for health checks (port-probe + /health endpoint)
  ├── Write ~/.agp/profiles/default.toml
  └── Print status table
```

**Bare-metal mode lifecycle:**
```
skyops up (bare-metal)
  ├── Check skyops.toml exists
  ├── skyops deps check (abort if missing)
  ├── Start postgres (if managed, not already running)
  ├── Start redis (if managed, not already running)
  ├── Start minio (if managed, not already running)
  ├── Wait for all deps healthy (TCP probe)
  ├── agp initdb (if first run)
  ├── agp serve --host ... --port ... (background)
  ├── Wait for /health 200
  ├── skyops db seed (if first run)
  ├── agp sweep-loop --interval-seconds 5 (background)
  ├── agp sweep-runtimes-loop --interval-seconds 10 (background)
  ├── agp runtime-work-loop ... (background, if runtime configured)
  ├── Write ~/.agp/profiles/default.toml
  └── Print status table
```

### Monitoring & Inspection

| Command | What It Does |
|---|---|
| `skyops health` | Deep health check: DB connection, Redis ping, MinIO bucket access, CP `/health`, runtime heartbeat ages. Aggregates all into pass/fail with detail. |
| `skyops logs [service]` | Tail logs. Docker: `docker compose logs`. Bare-metal: tail JSONL log files. Supports `--follow`. |
| `skyops metrics` | Quick summary from `AgpClient.observability_summary()`: jobs total, active runs, queue depth, runtimes |
| `skyops alerts` | Current active alerts from `AgpClient.observability_alerts()` |
| `skyops trace <job_id>` | Ordered execution trace from `AgpClient.job_trace()` |

### Work Dispatch (via AgpClient)

| Command | What It Does |
|---|---|
| `skyops send <agent_id> "task"` | Send work via `AgpClient.send()`. No `--server-url` flag needed — reads from profile. |
| `skyops watch <job_id>` | Poll job until terminal via `AgpClient.watch_job()` |
| `skyops jobs [--status X] [--agent Y]` | List jobs via `AgpClient.list_jobs()` |
| `skyops agents [--status X]` | List agents via `AgpClient.list_agents()` |
| `skyops interrupt <job_id>` | Interrupt job via `AgpClient.interrupt()` |
| `skyops fetch <artifact_id> [--content]` | Fetch artifact via `AgpClient.fetch_artifact()` |
| `skyops deliveries [--state X]` | List queue deliveries via `AgpClient.list_deliveries()` |

### Backup & DR

| Command | What It Does |
|---|---|
| `skyops backup create [path]` | Creates backup. Detects backend from config: SQLite → file copy; Postgres → `pg_dump` + S3 object snapshot. Replaces both `agp backup-create` and `scripts/phase3_backup_create.py`. |
| `skyops backup restore <path>` | Restores from backup. Detects backend and routes to appropriate restore logic. |
| `skyops backup list` | Lists available backup snapshots in the configured backup directory |
| `skyops backup validate` | Verifies artifact references resolve after restore |

### Security

| Command | What It Does |
|---|---|
| `skyops secrets show` | Shows which secrets are configured (values masked) |
| `skyops secrets generate` | Generates fresh random credentials for all services, writes to `skyops.local.toml` |
| `skyops secrets generate-k8s [path]` | Generates k8s Secret YAML. Replaces `generate_k8s_dev_secret.sh`. |
| `skyops secrets rotate-operator` | Rotates operator tokens via `AgpClient.rotate_operator_tokens()` |
| `skyops secrets rotate-runtime` | Rotates runtime tokens via `AgpClient.rotate_runtime_tokens()` |

### Upgrade

| Command | What It Does |
|---|---|
| `skyops upgrade status` | Current vs recorded version, migration state |
| `skyops upgrade apply` | Run pending migrations, mark new version |
| `skyops upgrade rollback` | Roll back to previous version metadata |

### Plugin Debugging

These commands migrate directly from the existing `agp host`, `agp adapter`, `agp plugin` sub-apps in `cli.py`. Same implementations, new home.

| Command | What It Does |
|---|---|
| `skyops host list-hosts` | List available terminal host kinds |
| `skyops host create --host-kind tmux --agent-id agt_demo` | Create/reuse a terminal session |
| `skyops host exists / health / send / read / snapshot / interrupt / terminate` | Full host debugging surface (9 commands) |
| `skyops adapter list-adapters` | List available adapter kinds |
| `skyops adapter bootstrap / inspect / run-once` | Adapter debugging (4 commands) |
| `skyops plugin run --host-kind tmux --adapter-kind codex --agent-id agt_demo --task "..."` | End-to-end plugin test |
| `skyops plugin repl` | Interactive REPL for plugin testing |

### Failure Drills

| Command | What It Does |
|---|---|
| `skyops drill run <scenario>` | Run a named failure injection drill. Migrates `agp failure-injection-run`. |
| `skyops drill list` | List available drill scenarios |
| `skyops drill full` | Run all drills sequentially (wraps `failure_drill.sh` logic) |

### Validation

| Command | What It Does |
|---|---|
| `skyops validate` | Lint compose and k8s manifest syntax. Replaces `validate_phase3_assets.py`. |
| `skyops smoke` | End-to-end smoke test against running stack. Replaces `phase3_stack_smoke.sh`. |
| `skyops k8s smoke` | Full kind cluster lifecycle + smoke. Replaces `k8s_smoke.sh`. |

### Direct DB Operations (bare-metal / emergency)

| Command | What It Does |
|---|---|
| `skyops queue reconstruct` | Rebuild queue from DB state |
| `skyops queue redrive` | Redrive stale in-flight deliveries |
| `skyops job block <job_id>` | Block a queued job |
| `skyops job unblock <job_id>` | Unblock a blocked job |
| `skyops sweep [leases\|runtimes\|idle\|draining]` | One-shot sweep operations |
| `skyops logs prune` | Prune old rotated log files |

## Migration: Scripts → skyops Commands

| Current Script | Becomes | Notes |
|---|---|---|
| `scripts/install_infra_tools*.sh` | `skyops deps install` | Platform detection built into skyops |
| `scripts/phase3_stack_up.sh` | `skyops up --mode docker` | |
| `scripts/phase3_stack_down.sh` | `skyops down --mode docker` | |
| `scripts/phase3_stack_smoke.sh` | `skyops smoke` | |
| `scripts/k8s_smoke.sh` | `skyops k8s smoke` | |
| `scripts/bootstrap_local_stack.py` | `skyops db seed` | Uses `agp.client.AgpClient` instead of raw httpx |
| `scripts/smoke_local_stack.py` | `skyops smoke` internals | Uses `agp.client.AgpClient` |
| `scripts/phase3_backup_create.py` | `skyops backup create` | |
| `scripts/phase3_backup_restore.py` | `skyops backup restore` | |
| `scripts/validate_backup_restore.sh` | `skyops backup validate` | |
| `scripts/failure_drill.sh` | `skyops drill full` | |
| `scripts/validate_phase3_assets.py` | `skyops validate` | |
| `scripts/generate_k8s_dev_secret.sh` | `skyops secrets generate-k8s` | |

Scripts that remain as-is:
- `scripts/wait_for_http.py` / `scripts/wait_for_tcp.py` — still useful inside k8s pod commands
- `scripts/run_local_codex.py` — dev convenience script, may be replaced by `skyops plugin run` over time

## Migration: `agp` CLI Commands → skyops

All 48 non-service commands move from `agp` to `skyops`:

| `agp` command (removed) | `skyops` command (new home) |
|---|---|
| `agp add-capability` | `skyops db seed` (declarative from config) |
| `agp send` | `skyops send` |
| `agp list-jobs` | `skyops jobs` |
| `agp list-agents` | `skyops agents` |
| `agp list-deliveries` | `skyops deliveries` |
| `agp watch-job` | `skyops watch` |
| `agp interrupt` | `skyops interrupt` |
| `agp fetch` | `skyops fetch` |
| `agp trace-job` | `skyops trace` |
| `agp observability` | `skyops metrics` |
| `agp observability-alerts` | `skyops alerts` |
| `agp observability-metrics` | `skyops metrics --prometheus` |
| `agp observability-dispatch-alerts` | `skyops alerts --dispatch` |
| `agp logs-control-plane` | `skyops logs control-plane` |
| `agp logs-runtime` | `skyops logs runtime <id>` |
| `agp logs-prune` | `skyops logs prune` |
| `agp backup-*` | `skyops backup *` |
| `agp upgrade-*` | `skyops upgrade *` |
| `agp security-*` | `skyops secrets *` |
| `agp failure-injection-run` | `skyops drill run` |
| `agp sweep` / `agp sweep-*` (one-shot) | `skyops sweep *` |
| `agp queue-reconstruct` / `agp queue-redrive` | `skyops queue *` |
| `agp job-block` / `agp job-unblock` | `skyops job block/unblock` |
| `agp runtime-register` / `agp runtime-claim-once` | `skyops runtime register/claim` (debug) |
| `agp runtime-work-once` | `skyops runtime work-once` (debug) |
| `agp host *` | `skyops host *` |
| `agp adapter *` | `skyops adapter *` |
| `agp plugin *` | `skyops plugin *` |

## Dependencies

skyops depends on:
- `agp` package (for `agp.client`, plugin factories, config types)
- `typer` (CLI framework)
- `tomllib` / `tomli` (config loading)
- `httpx` (via `agp.client`)
- `rich` (optional, for status tables and colored output)

skyops does NOT depend on: `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg` (except when running direct-DB commands that import from `agp` server-side modules).

## `skyops status` Output Format

```
$ skyops status

  AGP Stack (docker mode)
  Config: ./skyops.toml

  SERVICE          STATE     PORT    UPTIME    HEALTH
  ────────────────────────────────────────────────────
  postgres         running   5432    2h 15m    healthy
  redis            running   6379    2h 15m    healthy
  minio            running   9000    2h 15m    healthy
  control-plane    running   7860    2h 14m    healthy
  lease-sweeper    running   -       2h 14m    -
  runtime-sweeper  running   -       2h 14m    -
  runtime          running   -       2h 14m    -
  prometheus       running   9090    2h 14m    -
  grafana          running   3000    2h 14m    -

  Platform:  42 jobs completed, 3 running, 0 queued
  Agents:    2 active (agt_local, agt_python)
  Profile:   ~/.agp/profiles/default.toml
```

## Acceptance Criteria

- An operator can go from a fresh Ubuntu server to a running AGP stack using only `skyops init && skyops up`
- `skyops status` shows all services with health state
- `skyops send agt_local "hello"` works without `--server-url` or `--operator-token` flags
- `skyops up --mode docker` and `skyops up --mode bare-metal` both produce a working stack
- `skyops backup create` + `skyops backup restore` performs a full round-trip
- All existing script functionality is accessible through skyops commands
- `agp --help` shows only 5 service commands after the split
- The orc can `from agp.client import AgpClient` and use the same connection profile that skyops generates

## Implementation Order

1. **Phase A: AGP Client SDK** (prerequisite — see agp-client-sdk-prd.md)
   - Create `agp.client` package
   - Extract `*_via_api` → `AgpClient` methods
   - Extract `RuntimeClient` → `agp.client`
   - Implement `AgpProfile`
   - Slim down `cli.py` to 5 commands
   - Update all test imports

2. **Phase B: skyops skeleton**
   - Create `src/skyops/` package
   - `skyops.toml` config loading
   - `skyops init` (config generation)
   - `skyops status` (port-probe service detection)
   - `skyops config show/set`

3. **Phase C: Service lifecycle**
   - `skyops up / down / restart` (Docker mode first, bare-metal second)
   - `skyops db init / seed / status`
   - `skyops health`
   - Profile generation on `skyops up`

4. **Phase D: Operator commands**
   - Migrate dispatch commands (send, watch, jobs, agents, interrupt, fetch)
   - Migrate monitoring commands (metrics, alerts, logs, trace)
   - Migrate backup/security/upgrade commands
   - Migrate plugin debug commands (host, adapter, plugin)
   - Migrate drill/validation commands

5. **Phase E: Script retirement**
   - Update compose/k8s to work with skyops or standalone `agp` service commands
   - Remove or deprecate scripts that skyops replaces
   - Update documentation

## Test Strategy

- Unit tests for `skyops.toml` config loading and merging
- Unit tests for service detection (mock port probes)
- Integration tests for `skyops up --mode docker` (requires Docker)
- CLI invocation tests via `CliRunner` for each command group
- Existing 144 tests continue to pass (they test `agp` internals, not skyops)
- New skyops tests for the dispatch/monitoring commands that wrap `AgpClient`
