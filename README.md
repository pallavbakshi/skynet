# AGP — the Agentic Plane

> A control plane for running AI coding agents (Claude Code, Codex) **reliably** —
> durable jobs, automatic recovery, multi-agent coordination, and the ops to run it all.

This repository (`skynet`) contains **AGP**, a layered system that turns flaky, interactive
agent CLIs into a dependable execution platform you can build on.

---

## The problem

AI coding agents are powerful but hard to *operate*:

- They block on permission prompts, OAuth, and trust dialogs.
- They crash, hang, and silently lose context.
- They can't be queued, tracked, recovered, or coordinated at scale.
- Every system that uses them ends up reinventing supervision, state, and recovery.

**AGP separates the concerns:** a substrate that makes one agent supervisable, a platform that
makes execution reliable, and an orchestration surface that stays simple on top.

## The mental model

```
send work to an agent  →  get a reply or a job_id  →  track it  →  fetch the result + artifacts
```

You think in **agents, messages, and jobs**. AGP handles the queues, leases, restarts, and
infrastructure underneath.

---

## Quickstart

Requires Python ≥ 3.12 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups          # install everything (dev + test groups)
make test-smallops            # 142 offline parser tests, no external deps (~1.5s)
```

### Drive a real agent (the core capability, in 4 lines)

```python
from smallops import Session, TmuxMux, ClaudeCodeTui

with Session(mux=TmuxMux(), tui=ClaudeCodeTui()) as s:
    s.up(cwd="/path/to/repo")              # launch Claude Code in a tmux pane
    r = s.send("fix the bug in main.py")    # wait for completion, parse the response
    print(r.text)
```

This is **smallops** — the terminal-driving substrate. It boots the agent, delivers prompts
via files, polls the screen, auto-dismisses gates, detects idle/death, and returns a parsed
response. Zero third-party dependencies (Python stdlib only).

### Coordinate through the control plane

```bash
# One-time: initialize the DB schema, then start the control plane.
# (initdb/serve are dev-local infra commands — real, but hidden from `agp --help`.)
agp initdb
agp serve &                                 # http://localhost:7860

# Start a runtime that registers the `claude-dev` agent and executes its jobs.
# Set a provider key if you have one (OPENROUTER_API_KEY / OPENAI_API_KEY);
# otherwise Claude Code falls back to its OAuth login.
make claude-dev &

# Dispatch, wait, fetch. (fire-and-forget + wait is deterministic — see
# "For AI orchestrators" below for the safe contract.)
agp send claude-dev "refactor the auth module into a service" --fire-and-forget
agp wait <job_id> --poll-timeout 1800       # block until a terminal state
agp result <job_id>                         # fetch the result artifact
```

### Try it with no agent installed

```bash
# In-process stub host — no Claude Code, tmux, or API key required.
# Demonstrates the full dispatch → execute → artifact flow.
skyops debug plugin run inprocess default demo --task "hello, agp"
```

---

## Architecture

```
            ┌──────────────────────────────────────────────┐
   you ───► │  Orchestration   agp send / HTTP API          │
            │                  (agents, messages, jobs)     │
            └───────────────────────┬──────────────────────┘
                                    │  enqueue
            ┌───────────────────────▼──────────────────────┐
            │  Platform        control plane + runtime      │
            │                  jobs · runs · leases         │
            │                  durable state · artifacts    │
            │                  recovery · supervision       │
            └───────────────────────┬──────────────────────┘
                                    │  drive
            ┌───────────────────────▼──────────────────────┐
            │  Substrate      smallops                      │
            │                  drive + parse a TUI agent    │
            └───────────────────────┬──────────────────────┘
                                    │
                          tmux / wezterm / herdr  →  Claude Code · Codex
```

**Three Python packages, one-way dependency direction** (enforced by `make lint`):

```
skyops  ──►  agp  ──►  smallops     # never the reverse
```

- **`smallops`** — the substrate. Drives a TUI agent inside a terminal multiplexer and parses
  its screen. Stdlib-only. *Makes one agent supervisable.*
- **`agp`** — the platform. Control plane + agent runtime: durable jobs/runs/leases, state
  store, queue, artifact store, recovery, supervision. *Makes execution reliable.*
- **`skyops`** — the operator CLI. Deploys, backs up, upgrades, sweeps, and debugs the stack.
  *For the human running it.*

---

## Three entry points (and when to use each)

| Tool | Use it to | Audience |
|---|---|---|
| **`agp`** CLI / HTTP API | coordinate work — `send`, `wait`, `status`, `result`, `nudge`, `review` | orchestrators (humans **or** higher-order agents) |
| **`skyops`** CLI | operate the stack — `deploy`, `backup`, `upgrade`, `sweep`, `secrets`, `logs` | human operators |
| **`smallops`** library | drive a single agent TUI directly (`Session`) | runtime builders / library users |

An **orchestrator AI agent** addresses *logical agents* by ID; AGP maps each to the right
adapter and terminal — it never touches tmux or the agent CLI itself.

---

## For AI orchestrators

> You are an AI agent about to drive other AI agents through AGP. **Read this before dispatching
> anything.** The rules below prevent silent duplicate work and misread outcomes — and they are
> not obvious from `--help`.

### Prefer the API, not the CLI

If you can make HTTP calls, **use the control-plane API**; it returns JSON. The CLI returns text
you must parse, and its exit codes are lossy (see below). Authoritative schema:
[`openapi.yaml`](openapi.yaml). Base URL: `AGP_SERVER_URL` (default `http://localhost:7860`).
Auth: `Authorization: Bearer <token>` (operator token).

### The canonical loop

```
1. POST /messages/send     { agent_id, text, output_contract?, reply_to_message_id? }
   header: Idempotency-Key: <stable-per-logical-request>
   → { "job_id": "..." }                          # capture this

2. poll GET /jobs/{job_id}                        until status is terminal:
      terminal:      completed | failed | cancelled
      non-terminal:  accepted | queued | running | interrupt_requested | blocked

3. GET /artifacts/{result_artifact_id}/content    # the agent's output
     (on failure, fetch the role=failure_evidence artifact instead)
```

Also: `GET /jobs/{id}/events` (progress stream), `POST /jobs/{id}/interrupt` (stop),
`POST /jobs/{id}/handoff` (reroute), `GET /agents` / `GET /capabilities` (discovery). Full
surface in [`openapi.yaml`](openapi.yaml).

### Outcome: trust `status`, never the exit code

The job's **`status` field is the source of truth**:
- `completed` → success; read `result_artifact_id`.
- `failed` → read the `failure_evidence` artifact; the task did **not** succeed.
- `cancelled` / `blocked` / `interrupt_requested` → handle explicitly.

If you use the `agp` CLI anyway, its exit code is **lossy and unsafe to branch on**:
- exit `0` = completed **OR** the CLI stopped waiting while the job is still running;
- exit `1` = failed / auth / HTTP error.

So **never treat a CLI exit code as the outcome.** Always resolve the final `status` via
`GET /jobs/{id}` before acting. Deterministic CLI pattern: `agp send … --fire-and-forget`,
capture the `JOB_ID:` line, then `agp wait <id>`.

### Idempotency — don't cause duplicate work

Retries are expected, but a retry must **reuse the same `Idempotency-Key`** for the same logical
request. The CLI mints a fresh key per invocation, so **re-running `agp send` is NOT a safe
retry** — it starts a second job. Rule: on a transient failure of `POST /messages/send`, retry
the identical body with the identical `Idempotency-Key`. Do not re-dispatch a fresh send.

### Timeouts — detached ≠ failed

Two independent clocks:
- **Your poll window** (CLI `--poll-timeout`, default 300s) — how long *you* wait.
- **Server execution deadline** (~60 min, control-plane policy) — how long the *job* may run.

When your poll window expires the job is **still running** — it did not fail. **Do not resend.**
Keep polling `GET /jobs/{id}` (or `agp wait --poll-timeout 3600`) until `status` is terminal.

### Structured output — get JSON, not prose

To chain decisions programmatically, request a structured result instead of free text:

```
output_contract = {"format": "json", "json_schema": { ...your schema... }}
# CLI: agp send <agent> --output-contract '<json>'
# Built-in: agp send <agent> --review  → {"verdict","summary","findings":[{severity,description,file,line}]}
```

The result artifact then contains JSON you can parse directly — far more reliable than scraping
prose.

### Don't hallucinate

Exact flags, bodies, and shapes change. Before relying on any of the above, confirm against the
authoritative sources:
- **HTTP:** [`openapi.yaml`](openapi.yaml) — all paths + schemas.
- **CLI:** `agp <command> --help` (e.g. `agp send --help` lists every flag).
- **Terms:** [`research/glossary.md`](research/glossary.md).

Do not invent endpoints, flags, or status values not present in those sources.

---

## Capability matrix

| Category | What AGP provides |
|---|---|
| **Agents** | Claude Code, Codex — pluggable via the `Tui` Protocol |
| **Terminal backends** | tmux · wezterm · herdr — pluggable via the `Mux` Protocol |
| **Model providers** | Anthropic, OpenRouter / OpenAI-compatible (provider-env injection) |
| **Execution** | durable jobs / runs / leases · explicit state machines · automatic recovery · idle-reset timeouts with hard ceilings · gate auto-dismissal |
| **Coordination** | send / reply · fire-and-forget · interrupt · nudge · handoff · context-passing between jobs · structured output contracts (JSON schemas + a built-in code-review contract) |
| **Durability** | state store (Postgres / SQLite) · artifact store (S3 / local FS) · queue with inspect / reconstruct / redrive |
| **Operations** | init · db migrate · runtime deploy · backup / restore · upgrade / rollback · secrets rotation · lease & runtime sweeps · failure drills · logs · metrics · alerts · traces |
| **Reliability discipline** | enforced package boundary (import-linter **+** AST checker) · 142 offline parser property tests · Docker first-run qualification · a documented TUI version-upgrade process |

---

## Project layout

```
src/
  smallops/     # terminal substrate (drive + parse TUI agents) — stdlib only
  agp/          # control plane + runtime + adapters (the platform)
  skyops/       # operator CLI
smallops_tests/ # offline parser tests + live + docker qualification suites
research/       # PRDs, layer specs, the authoritative design docs
docs/           # operational process docs (e.g. TUI version upgrades)
migrations/     # DB migrations
scripts/        # ops + qualification scripts
```

## Development

```bash
uv sync --all-groups
make lint          # ruff + import-linter + the boundary checker
make test          # agp + smallops offline suites
make test-smallops # parser property tests (offline, default)
make test-live     # real Claude Code install required (SMALLOPS_LIVE=1)
make test-docker   # pristine Docker fresh-first-run qualification
make test-smallops-qualify  # Docker version-bump qualification + candidate corpora
```

Test markers: `offline` (default, deterministic) · `live` (real agent) · `docker` (pristine
first-run) · `judge` (LLM-judge oracle).

## Configuration

AGP reads provider credentials from the environment (e.g. `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`). Operator config lives in `skyops.toml`; generate a starter
with `skyops init`. The control-plane URL defaults to `http://localhost:7860` (override with
`AGP_SERVER_URL` or `agp send --server-url`).

## Further reading

The design lives in `research/`. Suggested order:

1. [`research/product-brief.md`](research/product-brief.md) — the one-page pitch
2. [`research/master-prd.md`](research/master-prd.md) — the full product PRD + shared vocabulary
3. [`research/glossary.md`](research/glossary.md) — canonical terms (Agent, Job, Run, Lease, …)
4. The layer specs and phase PRDs in `research/` for implementation depth

Operational docs (e.g. [`docs/smallops-version-upgrade.md`](docs/smallops-version-upgrade.md))
cover processes like qualifying a new Claude Code / Codex version.

---

## Status

AGP is **early and actively developed** — usable, but expect change.

- ✅ Core control plane, runtime, and substrate are implemented and self-consistent.
- ✅ Offline parser suite is green; the `skyops → agp → smallops` boundary is enforced by `make lint`.
- ⚠️ **No semver or changelog yet** — the public API may change without notice.
- ⚠️ Real-agent execution is validated via Docker qualification, not yet via unit tests on the
  `agp ↔ smallops` execution seam.
- ⚠️ `smallops` ships inside the `agp` distribution; standalone packaging is not yet available.

## Contributing

Contributions are welcome once the project stabilizes. In the meantime, the dev quickstart
above is the fastest way to explore. (A `CONTRIBUTING.md` and issue templates will follow.)

## License

[MIT](LICENSE). Contributions are accepted under the same terms.
