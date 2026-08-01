# Test Infra 01 — A dynamic, declarative test harness for the smallops TUI driver

**Status:** Ready to build · **Filed:** 2026-08-02
**Revised 2026-08-02:** replaced the golden-corpus approach with a dynamic,
declarative `{prompt, oracle}` harness (blessed snapshots track change instead
of catching regressions — see §3).
**Scope:** Test infrastructure **only**. See "Out of scope" (§12).

---

## TL;DR

Claude Code updates its TUI constantly; when markers, status-bar format, gate
wording, or response prefixes change, they silently break `smallops` (our driver
in `src/smallops/tui/claude_code/`). **Today nothing catches that** — no test
imports `smallops`, no test runs the parser, and there is no CI.

This doc specifies a **dynamic, declarative** harness. Test cases are
high-level specs `{prompt, oracle, environment}`. The harness drives smallops
end-to-end and verifies via **oracles** — preferring machine-checkable outcomes,
falling back to an LLM judge only when no outcome is checkable. There are **no
blessed golden snapshots**: assertions are *properties and outcomes*, which hold
across Claude Code versions, so the same spec keeps working as the TUI evolves.

**Approach decision: extend the existing scaffolding, do not rebuild.** The repo
already has a screen corpus, a capture script, `make capture` / `make test-parser`,
two Docker e2e scripts, and the `smallops` CLI. We rewire these and fill gaps.
New tests live in a new top-level **`smallops_tests/`** tree, isolated from the AGP
DB-test machinery and its root `tests/conftest.py` bootstrap. This is a deliberate
implementation adjustment from the earlier `tests/smallops/` placement: pytest
always loads `tests/conftest.py` for descendants of `tests/`, so a top-level
suite is the simpler robust isolation boundary.

**The biggest gotcha (read §3 before building):** a purely dynamic "send-and-check-
it's-non-empty" system is **too forgiving** — it catches catastrophic parser
breakage but misses *subtle* drift (dropped tool blocks, misclassified gates,
wrong status fields). Staying drift-sensitive without snapshots requires
**invariants + targeted canaries**, not just open-ended e2e.

---

## 1. Problem & goal

**Problem:** `smallops` interprets Claude Code's TUI by pattern-matching Unicode
markers, regexes, and gate text. TUI changes silently break it. Current state:
no test imports `smallops` or calls any parser function; the corpus and capture
tooling exist but nothing consumes them meaningfully; the Docker e2e scripts
aren't wired to `make`/CI; there's no `.github/` (no CI at all); and first-run
gates are stateful so they can only be tested in a pristine container.

**Goal:** a flexible, independent harness that is **low-maintenance AND
drift-sensitive**:
1. Detect TUI drift (structural) via deterministic invariants and targeted canaries.
2. Verify the live operation contract (`send`/`nudge`/`read`/`meta`) against a real Claude Code.
3. Reproduce the fresh-install path (first-run gates) that no dev machine can.
4. Verify *content* correctness via checkable outcomes, with an LLM judge only as a fallback.

---

## 2. Domain primer (just enough for a cold start)

**smallops** drives an interactive TUI agent inside a terminal pane. A
**`Session`** = a `Mux` (tmux or WezTerm; owns the pane and raw keystroke I/O)
composed with a **`Tui`** (`ClaudeCodeTui`; knows the launch command and how to
interpret the screen). Nothing runs until `up()`.

`Session` API (the surface under test):

| Family | Methods |
|---|---|
| Lifecycle | `up(cwd=, env=)`, `down()`, `reset()` |
| Drive | `send(prompt=, file=, timeout=)` → `Response`; `nudge(text)`; `interrupt()` |
| Observe | `meta()` → `Meta`; `is_alive()` → bool; `wait(timeout=)` → `IdleReason` |
| Read | `read(n=, since=)` (parsed); `peek(n=)` (raw screen) |

Key types: `IdleReason{READY,ERROR,GATE}`, `AgentState{WORKING,IDLE}`, `Status`
(model/tokens/context_pct/...), `Response`/`ParsedResponse` (`.text`, `.blocks`,
`.tool_uses`, `.raw`), exceptions `FatalGate`/`PaneDied`/`SendTimeout`/`BootstrapTimeout`.

**Gates** = blocking TUI prompts (permission/trust/onboarding/OAuth). smallops
auto-dismisses dismissible ones; OAuth is **fatal**. Patterns:
`src/smallops/tui/claude_code/_gates.py`.

**via-file delivery:** prompts are written to `/tmp/smallops/task-<id>.md` and a
short reference string is typed into the pane (never the prompt body itself).

The **driver under test** is the 5-file module `src/smallops/tui/claude_code/`
(`_markers.py`, `_parse.py`, `_classify.py`, `_gates.py`, `__init__.py`). Tests
target **smallops directly**, not the agp adapter. Full file:line map in §13.

---

## 3. Why not golden corpus (the principle)

A golden file you re-capture and re-author after every Claude Code update is not a
test — it's a rubber stamp. You end up *blessing whatever the new output is*, so
the suite silently tracks drift instead of failing on it. Snapshot-equality
against a moving target is the wrong primitive. **We do not assert exact-equality
against re-blessed captures.**

But the replacement has a subtlety: a fully dynamic e2e system with no
expectations only fails on **catastrophic** parser breakage (hangs, empty,
fatal gate). It misses **subtle** drift — a gate misclassified as READY, a
tool-use block dropped, status tokens parsed wrong but still "non-empty."
Behavioral checks like "response non-empty, reached READY" pass while the parser
is quietly wrong.

**Resolution — three assertion kinds, no snapshots:**

1. **Invariants** (deterministic, version-agnostic, always on): assert
   *properties* — `classify_idle` returns a valid `IdleReason`; after a real turn
   `parse_status` returns a populated model + tokens; idle classification is
   stable across repeated reads; tool blocks are detected when tools were used.
   These hold across versions and never need re-blessing.
2. **Canaries** (dynamic, targeted): prompts that *force a known parser surface*
   and assert a property of it — e.g., a prompt that forces a tool call → assert
   `parsed.tool_uses` is non-empty (catches tool-result prefix drift). Same
   coverage as the corpus, no blessed files.
3. **Outcome oracles** (correctness): prefer a **machine-checkable outcome**
   (exact string, file created, exit code, a test passing) over an LLM judge. The
   judge is the **fallback** only for open-ended tasks with no checkable outcome.

---

## 4. Architecture: the declarative harness (the spine)

The core abstraction is a **spec** and an **oracle registry**. The harness is
environment-agnostic; the same spec runs offline (against a fixed screen input),
live (real agent), or in Docker (pristine first-run) by changing `environment`.

```python
# smallops_tests/_harness.py
@dataclass
class Spec:
    prompt: str
    oracle: Oracle
    environment: str = "live"      # "offline" | "live" | "docker"
    mux: str = "tmux"
    timeout: float | None = None
    marks: tuple[str, ...] = ()    # pytest marks to apply

class Oracle:
    def verify(self, session, response) -> None: ...

# Outcome oracles (preferred — deterministic correctness):
class Exact(Oracle):       needle: str                     # "PONG" in response.text
class FileContent(Oracle): relpath: str; expected: str      # file created with content
class ExitZero(Oracle):    cmd: list[str]                   # a command the agent ran exits 0
class TestPasses(Oracle):  test_path: str                   # a pytest target the agent fixed
# Property oracles:
class Invariant(Oracle):   predicate: Callable              # custom property check
# Fallback (soft, content):
class Judge(Oracle):       rubric: str                      # LLM yes/no — soft signal
```

```python
def run_spec(spec: Spec) -> None:
    with _session_for(spec) as session:          # up()/down() per environment
        resp = session.send(spec.prompt, timeout=spec.timeout)
        _assert_invariants(session, resp)        # ALWAYS — see §6.1
        spec.oracle.verify(session, resp)        # outcome/property/judge
```

**Oracle priority (hard convention):** `Exact` / `FileContent` / `ExitZero` /
`TestPasses` / `Invariant` first; `Judge` only when none of those apply. A spec
that *can* be verified mechanically must not use `Judge`.

---

## 5. Existing scaffolding inventory

**Reuse (don't rebuild):**

| Asset | Location | New role |
|---|---|---|
| Screen corpus captures (`*.txt`/`*.raw`) | `smallops_tests/claude_code/corpus/` | **Inputs** for the thin offline property layer — never blessed outputs |
| Capture script | `scripts/capture-pane.sh` | Refresh offline inputs occasionally; not for blessing |
| `make capture` / `make test-parser` | `Makefile:627` / `:621` | Repoint `test-parser` at the new offline layer |
| Live Docker e2e (tmux / wezterm) | `scripts/docker/test-claude-code-{tmux.py,wezterm.sh}` | Basis for the fresh-first-run canaries |
| Docker runtime image | `Dockerfile`, `scripts/docker/runtime-entrypoint.sh`, `scripts/docker/wezterm.lua` | Untouched |
| `smallops` CLI | `src/smallops/cli.py` | Ad-hoc live probe |

**Gaps to fill:** no `smallops_tests/` tree; no harness/oracle module; no pytest
markers/`[tool.pytest]`; Docker scripts unwired + only 2 gates ad-hoc; no
artifact-on-failure; no CI. The existing `*.expected.json` golden files are
**retired** (do not assert against them) — keep the `*.txt`/`*.raw` captures only
as offline inputs.

---

## 6. Test layers by environment

| Layer | Environment | Agent? | Deterministic? | Cadence | Mark | Env gate |
|---|---|---|---|---|---|---|
| **A** Offline property | none | No | Yes | every commit | `offline` | none (default on) |
| **B** Live dynamic | dev machine | Yes | invariants yes; outcome yes | on-demand / pre-release | `live` | `SMALLOPS_LIVE=1` |
| **C** Fresh-first-run | Docker | Yes (pristine) | Yes | on-demand / nightly | `docker` | `SMALLOPS_DOCKER=1` + key |

`Judge` is **not a layer** — it's an oracle usable inside B (and C), always gated
behind invariants, soft by default, enabled via `SMALLOPS_JUDGE=1`.

### 6.1 Invariants (always asserted, every live/docker spec)
After a `send` returns:
- `meta().idle_reason == READY` and/or `is_alive()` True (turn completed, back at prompt).
- `resp.text` non-empty for reply-bearing prompts (the parser extracted a response).
- No exception (`FatalGate`/`PaneDied`/`SendTimeout`) and within `timeout`.
- When a real turn occurred: `meta().status.model` non-empty **and** `status.tokens > 0` (status bar parsed — catches status-format drift).
- Idle classification stable: repeated `meta()` while idle returns `READY` consistently.
- Raw/parsed consistency: `resp.parsed` did not throw and `resp.text` is consistent with `resp.raw` (the parser didn't silently drop the response).

These are the minimum drift-sensitive checks; they run before the spec's oracle.

### 6.2 Layer A — Offline property (thin, fast)
**Location:** `smallops_tests/claude_code/test_parser_properties.py`, mark `offline`.
Parametrize over the existing corpus `*.txt` as **inputs** (run through
`strip_ansi` + `normalize_screen`). Assert **invariants only** — never equality
to `*.expected.json`:
- parser/classifier do not throw on any capture;
- `classify_idle` returns a valid `IdleReason`;
- for visibly-ready screens → `READY`; for gate screens → `GATE` and
  `gate_response()` non-empty; for shell-returned screens → not alive / `ERROR`;
- `parse_status` returns well-typed fields (model str, tokens int ≥ 0).

Purpose: a 2-second, no-agent smoke test for gross parser breakage on every
commit. It is **not** the golden anti-pattern — no equality, no re-blessing.

### 6.3 Layer B — Live dynamic (real agent)
**Location:** `smallops_tests/test_live.py` (+ spec catalog in
`smallops_tests/specs/`), mark `live`. A session fixture `up()`s on a temp cwd,
`down()`s in teardown; parametrize mux (tmux first). Specs are the canaries +
verifiable tasks in §10. Invariants (§6.1) always run, then the oracle.

### 6.4 Layer C — Fresh-first-run (Docker)
**Location:** `smallops_tests/test_firstrun.py`, mark `docker`. Generalize the
existing Docker scripts into asserted canaries (don't reinvent the container
infra). **Setup per test:** throwaway container, empty Claude config dir,
`ANTHROPIC_API_KEY` set, **no** `claude -p` pre-completion. Coverage: drive
bootstrap through each `AUTO_GATE_PATTERNS` category → assert dismissed → `READY`;
plus the fatal path → assert `FatalGate`. Gates are deterministic (fixed text →
fixed dismissal), so no judge here.

---

## 7. Cross-cutting concerns

### 7.1 Pytest markers + config (none exist today)
Add to `pyproject.toml`:
```toml
[tool.pytest.ini_options]
markers = [
  "offline: deterministic parser property tests, no live agent (default on)",
  "live: real Claude Code install required (SMALLOPS_LIVE=1)",
  "docker: pristine Docker fresh-first-run (SMALLOPS_DOCKER=1 + ANTHROPIC_API_KEY)",
  "judge: uses the LLM-judge oracle, soft signal (SMALLOPS_JUDGE=1 + ANTHROPIC_API_KEY)",
]
addopts = "-ra --strict-markers"
```

### 7.2 Isolated `smallops_tests/conftest.py`
The root `tests/conftest.py` forces AGP SQLite DB redirection and asserts
`agp.config` import ordering — irrelevant to smallops and can fail if smallops is
imported first. Keep smallops tests isolated under top-level `smallops_tests/` with their
own conftest. This intentionally avoids AGP root `tests/conftest.py`. Env-gating (skip unless env set, so `make test` stays fast):
```python
import os, pytest
def pytest_collection_modifyitems(config, items):
    gates = {
        "live":   ("SMALLOPS_LIVE",   "set SMALLOPS_LIVE=1"),
        "docker": ("SMALLOPS_DOCKER", "set SMALLOPS_DOCKER=1 (+ ANTHROPIC_API_KEY)"),
        "judge":  ("SMALLOPS_JUDGE",  "set SMALLOPS_JUDGE=1 (+ ANTHROPIC_API_KEY)"),
    }
    for item in items:
        for mark, (env, why) in gates.items():
            if mark in item.keywords and not os.environ.get(env):
                item.add_marker(pytest.mark.skip(reason=why))
```

### 7.3 Artifact-on-failure
A `pytest_runtest_makereport` hook in `smallops_tests/conftest.py` that, on any
`live`/`docker`/`judge` failure, dumps `Session.peek()` and the last
`Response.raw` to `test-artifacts/<test-id>-<ts>/`. This capture distinguishes
"drift in `_markers.py`" from "smallops logic bug."

### 7.4 Judge helper (`smallops_tests/_judge.py`)
```python
@dataclass
class Verdict: aligned: bool; reason: str
def judge(prompt: str, output: str, rubric: str, *, model: str | None = None) -> Verdict: ...
```
Anthropic Python SDK, a current small/fast (Haiku-tier) model, structured
**yes/no + one-line reason** via tool use, temperature 0. **Soft by default**
(verdict recorded, not failing) unless `SMALLOPS_JUDGE_STRICT=1`. Pick the current
model/SDK at build time per latest Anthropic guidance; do not hard-code stale IDs.

### 7.5 Makefile targets
`make test` (fast default; A included, B/C/judge self-skip) · `make test-smallops`
(`-m offline`) · `make test-live` (`SMALLOPS_LIVE=1 -m live`) · `make test-docker`
(`SMALLOPS_DOCKER=1 -m docker`). Repoint `make test-parser` at Layer A.

---

## 8. Hard rules (non-negotiables)

1. **No blessed snapshots.** Never assert exact-equality against re-captured/
   re-authored expected output. Assert properties and outcomes.
2. **Verifiable oracle before judge.** A spec that can be checked mechanically
   (exact/file/exit/test-passes/invariant) must not use `Judge`.
3. **Invariants always run**, before the oracle, on every live/docker spec.
4. **Judge is gated and soft.** Only called after invariants pass; soft signal
   unless `SMALLOPS_JUDGE_STRICT=1`.
5. **Target smallops directly** (`from smallops import ...`), not the agp adapter.
6. **Fresh-first-run must NOT pre-bake onboarding.** No `claude -p` first-run
   completion in Layer C; set `ANTHROPIC_API_KEY` (avoid OAuth fatal gate) but
   leave the Claude config dir pristine so gates fire.
7. **Never touch the authed runtime container** — use separate throwaway containers.
8. **Env-gated and skipped by default** — `make test` never spawns an agent,
   Docker, or an LLM call.
9. **Dump raw screen artifacts on every failure.**
10. **Normalize corpus inputs consistently** — run captures through `strip_ansi` +
    `normalize_screen` exactly as `_read_screen` does before parsing.

---

## 9. Build phases (do in this order; each is independently shippable)

**Phase 1 — Harness core + oracles.** `_harness.py` (`Spec`, `Oracle` registry,
`run_spec`), the invariant suite (§6.1), isolated conftest, markers/config, env
gating, artifact-on-failure, Makefile targets. Everything else depends on this.

**Phase 2 — Layer A (offline property).** Thin, fast, no agent. Repurpose corpus
captures as inputs; retire `*.expected.json`. Gives a commit-time smoke test.

**Phase 3 — Layer B (live dynamic).** Session fixture + the canary + verifiable
spec catalog (§10). Requires a local Claude Code. This is where most drift
detection actually lives.

**Phase 4 — Layer C (Docker fresh-first-run).** Generalize the Docker scripts;
systematic gate canaries + fatal-gate detection. Biggest infra piece.

**Phase 5 — Judge fallback.** Last and most optional; only specs that need it.

---

## 10. Declarative spec catalog (seed the suite with these)

**Canaries — probe a parser surface, assert a property (Layer B/C):**

| Prompt | Forces | Oracle / invariant | Catches |
|---|---|---|---|
| `"Reply with exactly: PONG"` | a turn | `Exact("PONG")` + invariants | basic send→read loop |
| `"Read README.md and quote its first line"` | a tool call | `Invariant: parsed.tool_uses non-empty` | tool-result prefix `⎿` drift |
| two sends, read the 2nd | marker/separator | `Invariant: read(since=marker)` isolates 2nd turn | response-prefix / separator drift |
| after bootstrap + each turn | ready prompt | `Invariant: classify_idle == READY` | prompt-marker `❯` drift |
| any real turn, then observe | status bar | `Invariant: status.model and tokens>0` | status-format drift |
| (Layer C) trigger trust dialog | a gate | `Invariant: GATE + gate_response() non-empty → READY` | gate-pattern drift |

**Verifiable tasks — machine-checkable correctness (Layer B):**

| Prompt | Oracle |
|---|---|
| `"Create hello.txt containing 'world'"` | `FileContent("hello.txt", "world")` |
| `"Add a function double(x) to calc.py that returns x*2"` | `ExitZero(["python","-c","import calc; assert calc.double(3)==6"])` |
| `"Make tests/test_fixture_example.py pass"` | `TestPasses("tests/test_fixture_example.py")` |
| `"Reply with exactly: ZXQ77"` | `Exact("ZXQ77")` |

**Judge-only (fallback, Layer B, soft):** `"Explain this repo in two sentences"`
→ `Judge("Does the response describe a software project?")` — only because no
mechanical oracle exists.

---

## 11. Decisions to make (before/during build)

- **D1 — How thin is Layer A?** Keep just enough captures to smoke-test the parser
  offline (recommended), vs. drop fixed inputs entirely and rely on Layer B/C.
  Lower stakes than the old golden approach since nothing is blessed.
- **D2 — Claude Code version strategy in Docker.** Pin for reproducibility vs.
  allow-latest to catch current drift; whether to matrix versions.
- **D3 — Mux backend scope.** tmux first, add wezterm later (recommended).
- **D4 — Corpus location.** Relocated under `smallops_tests/` for ownership
  clarity. Low stakes now (inputs only).
- **D5 — CI host.** No `.github/` exists; out of scope to stand up now, but design
  markers/env gating so a future nightly can run `-m docker`/`-m live` unattended.
- **D6 — Judge model.** Pick a current Haiku-tier model at build time.

---

## 12. Out of scope (do NOT do in this PRD)

- **`docs/improvement_01.md`** (the `_read_screen` observe-method side-effect fix)
  is a **separate task**. Do not fold it in. Its tests can later ride on this
  harness, but building the harness must not depend on that fix.
- The agp adapter's tests (`tests/plugins/`, `tests/mvp_flow/`) and skyops tests — untouched.
- Rewriting `smallops` itself. Change source only where a test genuinely requires a
  seam (prefer testing through the public `Session`/`ClaudeCodeTui` API).

---

## 13. Reference index

| What | Where |
|---|---|
| Driver under test (the drift surface) | `src/smallops/tui/claude_code/` (`_markers.py`, `_parse.py`, `_classify.py`, `_gates.py`, `__init__.py`) |
| `Session` API (lifecycle/drive/observe/read) | `src/smallops/__init__.py` |
| Types (`IdleReason`, `Status`, `Response`, exceptions) | `src/smallops/_types.py` |
| Screen normalization the tests must match | `strip_ansi`, `normalize_screen` in `src/smallops/_util.py`; `_read_screen` in `src/smallops/__init__.py:89` |
| Gate pattern tables | `src/smallops/tui/claude_code/_gates.py` |
| Corpus captures (now offline **inputs**, not blessed outputs) | `smallops_tests/claude_code/corpus/` (`*.txt`, `*.raw`; retired `*.expected.json`) |
| Capture script | `scripts/capture-pane.sh` |
| Live Docker e2e (tmux / wezterm) | `scripts/docker/test-claude-code-tmux.py`, `scripts/docker/test-claude-code-wezterm.sh` |
| Docker runtime image | `Dockerfile`, `scripts/docker/runtime-entrypoint.sh`, `scripts/docker/wezterm.lua` |
| Existing Makefile test targets | `Makefile:618` (`test`), `:621` (`test-parser`), `:627` (`capture`) |
| Existing pytest bootstrap (agp DB; do not pollute smallops) | `tests/conftest.py` |
| Related, separate task | `docs/improvement_01.md` |
