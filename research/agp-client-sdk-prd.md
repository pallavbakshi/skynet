# Product Requirements Document

## Document
AGP Client SDK PRD

## Version
0.1 Draft

## Purpose
Extract a clean Python SDK (`agp.client`) from the current monolithic `cli.py` so that three consumers — the orc (orchestrator agent), skyops (operator CLI), and the runtime supervisor — can talk to the AGP control plane through a shared, well-defined client library.

This PRD exists to answer:
- what the SDK surface looks like
- how connection profiles replace per-command `--server-url` / `--operator-token` flags
- how the existing codebase is refactored without disruption
- what the `agp` CLI binary becomes after the extraction

## Why This Exists

Today, `src/agp/cli.py` is a 2153-line monolith that mixes:
- 5 service entrypoints (what k8s/compose containers run)
- 17 API client wrapper commands (thin shells over `httpx.Client` calls)
- 19 operator/admin commands (direct DB access)
- 15 plugin debug commands (local terminal host interaction)
- 18 `*_via_api` helper functions that are already an SDK in disguise
- `RuntimeClient` in `runtime.py` that is a separate partial SDK for the runtime side

This creates three problems:
1. **The orc has no clean way to talk to the control plane.** It would need to either shell out to `agp send --server-url ... --operator-token ...` on every call, or import internal functions from `cli.py` that were never designed as a public API.
2. **Every consumer gets every dependency.** A runtime container that only needs `httpx` to talk to the CP gets `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg`, `boto3`, and the entire control-plane server pulled in.
3. **There is no connection profile.** Every CLI command that talks to the CP requires `--server-url` and `--operator-token` flags. There is no persistent context.

## Goal

Provide a Python SDK that:
- wraps all control-plane API operations in a single `AgpClient` class
- loads connection context from a profile file or environment variables
- is usable by the orc (programmatically), skyops (as its internal client), and tests (directly)
- has no dependency on the server-side packages (no fastapi, no sqlalchemy, no uvicorn)

## Non-Goals

- Building a REST framework or code-generating the SDK from OpenAPI
- Supporting non-Python SDK consumers (Go, TypeScript, etc.)
- Changing the control-plane API surface itself
- Adding new API endpoints

## Scope

This PRD covers:
- The `AgpClient` class and its methods
- Connection profiles (`~/.agp/profiles/*.toml` or env vars)
- Refactoring `cli.py` into a lean service-only CLI
- Moving `*_via_api` functions into the SDK
- Decoupling `RuntimeClient` from server-side imports
- Test migration strategy

This PRD does not cover:
- The skyops operator CLI (separate PRD)
- New API endpoints on the control plane
- Changes to the runtime supervisor's execution logic
- Plugin or adapter changes

## Architecture

### Package Structure

```
src/
  agp/
    client/
      __init__.py          # exports AgpClient, RuntimeClient, AgpProfile
      _profile.py          # profile loading (TOML + env vars)
      _operator.py         # AgpClient (operator-side API calls)
      _runtime.py           # RuntimeClient (runtime-side API calls)
    cli.py                 # SLIM: only service entrypoints (5 commands)
    control_plane.py       # unchanged
    runtime.py             # RuntimeSupervisor stays; RuntimeClient import redirects to agp.client
    ... (rest unchanged)
```

### What `agp.client` Exports

```python
from agp.client import AgpClient, AgpProfile, RuntimeClient, RuntimeIdentity
```

### `AgpProfile` — Connection Context

```python
@dataclass
class AgpProfile:
    server_url: str                     # e.g. "http://server:7860"
    token: str | None = None            # operator or runtime bearer token
    name: str = "default"               # profile name for display

    @classmethod
    def load(cls, name: str = "default") -> "AgpProfile": ...

    @classmethod
    def from_env(cls) -> "AgpProfile": ...
```

**Resolution order:**
1. Explicit constructor args (programmatic use)
2. `AGP_SERVER_URL` + `AGP_OPERATOR_TOKEN` env vars (container/CI use)
3. `~/.agp/profiles/{name}.toml` file (operator workstation use)
4. Fallback: `http://127.0.0.1:7860`, no token (local dev)

**Profile file format:**
```toml
# ~/.agp/profiles/prod.toml
server_url = "http://control-plane.agp.svc:7860"
token = "tok_operator_abc123"
```

`skyops up` will auto-generate `~/.agp/profiles/default.toml` when it starts the stack (covered in skyops PRD).

### `AgpClient` — Operator SDK

```python
class AgpClient:
    def __init__(self, profile: AgpProfile | None = None, http_client: httpx.Client | None = None): ...
    def close(self) -> None: ...
    def __enter__(self) -> "AgpClient": ...
    def __exit__(self, ...) -> None: ...

    # Health
    def health(self) -> dict: ...

    # Work dispatch
    def send(self, target_type: str, target_id: str, text: str, **kwargs) -> dict: ...
    def interrupt(self, job_id: str) -> dict: ...

    # Inspection
    def get_job(self, job_id: str) -> dict: ...
    def list_jobs(self, **filters) -> dict: ...
    def list_agents(self, **filters) -> dict: ...
    def list_deliveries(self, **filters) -> dict: ...
    def watch_job(self, job_id: str, poll_interval: float = 0.25, max_polls: int | None = None) -> list[dict]: ...

    # Artifacts
    def fetch_artifact(self, artifact_id: str, content: bool = False) -> dict: ...
    def list_job_artifacts(self, job_id: str, role: str | None = None) -> dict: ...
    def list_run_artifacts(self, run_id: str, role: str | None = None) -> dict: ...

    # Observability
    def observability_summary(self) -> dict: ...
    def observability_alerts(self) -> dict: ...
    def observability_metrics(self) -> str: ...
    def observability_dispatch_alerts(self) -> dict: ...
    def job_trace(self, job_id: str) -> dict: ...
    def logs_control_plane(self, limit: int = 100) -> dict: ...
    def logs_runtime(self, runtime_id: str, limit: int = 100) -> dict: ...

    # Security
    def auth_status(self) -> dict: ...
    def rotate_operator_tokens(self, **kwargs) -> dict: ...
    def rotate_runtime_tokens(self, **kwargs) -> dict: ...
```

Each method is a 1:1 migration of the existing `*_via_api` functions, with the `httpx.Client` and auth headers managed internally by `AgpClient`.

### `RuntimeClient` — Runtime SDK

The existing `RuntimeClient` class in `runtime.py` moves to `agp.client._runtime`. It is decoupled from:
- `agp.config.settings` — logging becomes optional (pass a logger or disable)
- `agp.db.current_release_version` — `release_version` becomes a plain string parameter with a sensible default

The `RuntimeSupervisor` in `runtime.py` continues to import `RuntimeClient` from `agp.client` (with a compat re-export in `agp.runtime` via `__getattr__`).

### How the Orc Uses It

```python
from agp.client import AgpClient

# Loads from AGP_SERVER_URL env var or ~/.agp/profiles/default.toml
client = AgpClient()

result = client.send("agent", "agt_python", "write the function")
job = client.watch_job(result["job_id"])[-1]["job"]
artifact = client.fetch_artifact(job["result_artifact_id"], content=True)
print(artifact["content"])
```

### How skyops Uses It

```python
from agp.client import AgpClient, AgpProfile

profile = AgpProfile(server_url="http://server:7860", token=config.operator_token)
client = AgpClient(profile=profile)
client.send("agent", "agt_python", task_text)
```

### How Tests Use It

```python
from agp.client import AgpClient

# Tests pass the FastAPI TestClient directly
client = AgpClient(http_client=self.client)
result = client.send("agent", "agt_test", "test task")
```

This preserves the existing test pattern where `self.client` is a `TestClient`. The `AgpClient` constructor accepts an optional `http_client` parameter for injection.

## What `agp` CLI Becomes

After extraction, `cli.py` contains only service entrypoints — the commands that k8s/compose containers invoke:

| Command | What it does |
|---|---|
| `agp serve` | Run the control-plane API server |
| `agp initdb` | Initialize database schema |
| `agp runtime-work-loop` | Continuous runtime worker (claim + execute loop) |
| `agp sweep-loop` | Continuous lease sweeper |
| `agp sweep-runtimes-loop` | Continuous runtime health sweeper |

All other commands (48 of them) move to `skyops` (separate PRD).

The `pyproject.toml` entry point stays as `agp = "agp.cli:app"`.

## Migration Strategy

Since everything is in dev stage (nothing in production), this is a clean break:

1. **Create `src/agp/client/` package** with `AgpProfile`, `AgpClient`, `RuntimeClient`, `RuntimeIdentity`.
2. **Move the 18 `*_via_api` functions** from `cli.py` into `AgpClient` methods. Keep the function bodies identical — just wrap them as methods with `self._client` and `self._headers`.
3. **Move `RuntimeClient` and `RuntimeIdentity`** from `runtime.py` to `agp.client._runtime`. Add `__getattr__` compat shim in `runtime.py` for existing imports.
4. **Move `watch_job_until_terminal`** into `AgpClient.watch_job`.
5. **Move `_build_headers`** into `AgpProfile` or `AgpClient` internals.
6. **Strip `cli.py`** to only the 5 service commands plus `initdb`. Remove all operator/debug commands, plugin sub-apps, and API client wrappers.
7. **Move helper functions** used by operator commands (`create_backup_snapshot`, `run_failure_injection_scenario`, etc.) into a new internal module (`agp._ops_helpers` or directly into skyops).
8. **Update test imports.** The test file imports 19 SDK symbols from `agp.cli` — these change to `from agp.client import AgpClient` plus method calls. The 10 operator function imports change to wherever those functions land (likely `agp._admin` or `skyops._ops`).
9. **Update `runtime.py`** to import `RuntimeClient` from `agp.client` with a compat `__getattr__` fallback.
10. **Update `scripts/`** that duplicate HTTP client logic (`bootstrap_local_stack.py`, `smoke_local_stack.py`) to use `AgpClient` instead of raw `httpx`.

## Dependencies

`agp.client` requires only:
- `httpx` (HTTP client)
- `tomllib` / `tomli` (profile loading — stdlib in 3.11+)

It must NOT import: `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg`, `redis`, `boto3`, `typer`.

## Acceptance Criteria

- The orc can `from agp.client import AgpClient` and send work, watch jobs, and fetch artifacts without importing any server-side code.
- `AgpClient` loads connection context from env vars or profile files without per-call `--server-url` flags.
- `RuntimeClient` works without importing `agp.config` or `agp.db`.
- All 144 existing tests pass after the migration (with updated imports).
- `agp --help` shows only 5 service commands.
- `scripts/smoke_local_stack.py` and `scripts/bootstrap_local_stack.py` use `AgpClient` instead of raw `httpx`.

## Test Strategy

- Unit tests for `AgpProfile.load()` with env vars, profile files, and fallback
- Unit tests for `AgpClient` methods using a mock `httpx.Client`
- Existing 144 tests migrated to import from `agp.client` — no logic changes, only import paths
- Integration test: `AgpClient` against a real `TestClient`-backed control plane
