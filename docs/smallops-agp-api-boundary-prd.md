# PRD: Enforce a public-API boundary between `agp` and `smallops`

- **Status:** Draft, ready to implement
- **Owner:** TBD
- **Scope:** `src/agp/plugins/`, `src/smallops/` public surface, lint config
- **Risk:** Low — import-path refactor + one lint contract. No behavior change, fully reversible.

---

## 1. Context (read this first)

This repo (`skynet`) ships a product called **AGP — "Agentic Plane"** (`pyproject.toml:6`), a control plane for reliably executing and coordinating AI coding agents (Claude Code, Codex). It is structured as three Python packages in one repo:

| Package | Layer | Responsibility |
|---|---|---|
| `smallops` (`src/smallops/`) | Substrate | Drives a TUI agent inside a terminal pane (tmux/wezterm) and parses its screen. The thing that makes an agent supervisable. **Zero third-party deps (stdlib only).** |
| `agp` (`src/agp/`) | Platform | The control plane + agent runtime. Heavy deps (fastapi/uvicorn/sqlalchemy/psycopg/redis/pydantic). |
| `skyops` (`src/skyops/`) | Ops CLI | Infrastructure lifecycle over the agp stack. |

**Dependency direction is one-way and must stay one-way:** `skyops → agp → smallops`. `smallops` must never import `agp` (this already holds today — verified).

**`smallops` already has a clean public SDK.** Its public entrypoint is the `Session` class (`src/smallops/__init__.py`):

```python
from smallops import Session, TmuxMux, ClaudeCodeTui
with Session(mux=TmuxMux(), tui=ClaudeCodeTui()) as s:
    s.up(cwd="/path")
    r = s.send("fix the bug")
```

Public surface is defined by `__all__` in `src/smallops/__init__.py`: `Session, TmuxMux, WezTermMux, ClaudeCodeTui, CodexTui`, plus the type hierarchy (`SessionInfo, Response, ParsedResponse, Block, BlockKind, Status, IdleReason, Config, Meta, AgentState`) and exceptions (`SmallopsError, SendTimeout, BootstrapTimeout, PaneDied, FatalGate`).

**The decoupling seam already exists in `agp`'s test suite.** agp tests do **not** run against real smallops. They run against `InProcessTerminalHost` (`src/agp/plugins/inprocess.py:20`) — a fake in-process host implementing the `TerminalHost` ABC (`src/agp/runtime/_abc.py`). **Zero agp tests `import smallops`** (the only mention is a stray comment at `tests/plugins/claude_code/test_via_file.py:45`). So the architectural seam we need is *already built and in use*. This PRD is not about building a seam — it is about closing a private-import leak and locking the boundary.

---

## 2. Problem

`agp` depends on `smallops`'s **private modules** (any `_*`-prefixed leaf). This is the real source of cross-package "blast radius":

> A developer refactors smallops internals (renames a `_types` symbol, moves `strip_ansi`, relocates `_classify`) → agp's adapter module fails to import → **agp's entire test collection fails** for anything that imports the adapter (module-level deps), or `inspect_output` breaks at call time (the `_classify` reach-in). One internal smallops change takes down agp's whole suite.

### Evidence — every private reach-in (verified)

| File | Line(s) | Imports | Status |
|---|---|---|---|
| `src/agp/plugins/claude_code/adapter.py` | 41–53 | `from smallops._types import BootstrapTimeout, FatalGate, PaneDied, SendTimeout` | **private path, public symbols** (all 4 are already in `smallops.__all__`) |
| `src/agp/plugins/claude_code/adapter.py` | 53 | `from smallops._util import strip_ansi` | **private path**; `strip_ansi` is in the `smallops` namespace (`__init__.py:43`) but **not in `__all__`** |
| `src/agp/plugins/claude_code/adapter.py` | 152 | `from smallops.tui.claude_code._classify import ends_with_prompt, is_shell_returned` | **fully private** — not exported anywhere |
| `src/agp/plugins/codex/adapter.py` | 43–55 | `from smallops._types import ...` (same 4 symbols) | private path, public symbols |
| `src/agp/plugins/codex/adapter.py` | 55 | `from smallops._util import strip_ansi` | private path |
| `src/agp/plugins/codex/adapter.py` | 162 | `from smallops.tui.codex._classify import ends_with_prompt, is_shell_returned` | fully private |
| `src/agp/plugins/_smallops_host.py` | 20 | `from smallops._protocols import Mux` | private path; `Mux`/`Tui` in namespace but not in `__all__` |

For reference, these agp imports are already on the public path and need no change: `from smallops import Session, ClaudeCodeTui, CodexTui, Config, SessionInfo, TmuxMux, WezTermMux` (adapters, `_smallops_host.py`, `plugins/__init__.py`).

---

## 3. Goals

1. **Eliminate every private-module import** of `smallops` from `agp`. After this work, no agp source imports any `smallops.*._*` or `smallops.tui.<x>._classify` module.
2. **Promote the genuinely-private symbols agp needs** (`ends_with_prompt`, `is_shell_returned`) into `smallops`'s documented public API, so agp depends on a *contract*, not internals.
3. **Lock the boundary with tooling** so the leak cannot regress: an enforced contract that fails `make lint` / CI if agp imports smallops internals.
4. **No behavior change.** Runtime behavior, parser output, and the public `Session` API are unchanged.

## 4. Non-Goals (do NOT do these)

- ❌ **Do not split `smallops` into a separate package, distribution, or repo.** The blast radius is fixed by closing the private-import leak + enforcing a contract; a packaging split is explicitly deferred (see §9) and out of scope here.
- ❌ Do not change smallops's parser/classifier logic or runtime behavior.
- ❌ Do not introduce a new abstraction layer or rewrite the existing `TerminalHost`/`AgentAdapter` seam.
- ❌ Do not change the public `Session` API surface.

---

## 5. Solution

Three steps, in order.

### 5.1 Promote the needed symbols to `smallops`'s public API

In `src/smallops/__init__.py`:

- **Add to `__all__`:** `strip_ansi` (already imported at `__init__.py:43`), and `Mux`, `Tui` (the Protocols, already imported from `_protocols`). These are already in the namespace; this just formally exports them.

- **Promote `ends_with_prompt` and `is_shell_returned`** — currently private in `smallops/tui/claude_code/_classify.py` and `smallops/tui/codex/_classify.py`. **Recommended approach:** add them as **methods on the `Tui` Protocol and the concrete `ClaudeCodeTui`/`CodexTui` classes**, alongside the existing `classify_idle`/`parse_response`/`gate_response` protocol methods. The adapter then calls `self._tui.ends_with_prompt(clean)` instead of importing a private function. This keeps TUI-specific classification logic encapsulated on the Tui object and is the most idiomatic.

  *Minimal alternative (if you want the smallest diff):* re-export the functions from the public `smallops.tui.claude_code` / `smallops.tui.codex` package namespace and add them to `__all__`. Acceptable, but the method approach is preferred.

  The implementation in `_classify.py` stays where it is; only its *visibility* changes.

### 5.2 Rewrite every agp import to go through the public surface

For each row in the §2 evidence table, change the import path:

| Before (private) | After (public) |
|---|---|
| `from smallops._types import PaneDied, SendTimeout, BootstrapTimeout, FatalGate` | `from smallops import PaneDied, SendTimeout, BootstrapTimeout, FatalGate` |
| `from smallops._util import strip_ansi` | `from smallops import strip_ansi` |
| `from smallops._protocols import Mux` | `from smallops import Mux` |
| `from smallops.tui.claude_code._classify import ends_with_prompt, is_shell_returned` | call via the Tui instance: `tui.ends_with_prompt(clean)`, `tui.is_shell_returned(clean)` (per §5.1) |
| (same for `smallops.tui.codex._classify`) | (same, via the Codex `Tui` instance) |

Files to edit (complete list):
- `src/agp/plugins/claude_code/adapter.py` (imports + the `inspect_output` method at line ~150)
- `src/agp/plugins/codex/adapter.py` (imports + `inspect_output` at line ~160)
- `src/agp/plugins/_smallops_host.py` (line 20)

After this step, this grep must return **nothing**:
```bash
grep -rnE 'from smallops\._|import smallops\._' src/agp/
grep -rnE 'smallops\.tui\.[a-z_]+\._' src/agp/
```

### 5.3 Enforce the boundary with an import-linter contract (new config)

There is **no** import-linter / tach config in the repo today — this is new. Add `import-linter` to dev deps and a contract in `pyproject.toml`:

```toml
# [tool.uv] / dependency-groups — add:
#   dev = [ ..., "import-linter>=2,<3" ]

[tool.importlinter]
root_packages = ["agp", "smallops", "skyops"]

[[tool.importlinter.contracts]]
name = "agp depends only on smallops public API"
type = "forbidden"
source_modules = ["agp"]
forbidden_modules = [
  "smallops._types",
  "smallops._util",
  "smallops._protocols",
  "smallops._poll",
  "smallops.tui.claude_code._classify",
  "smallops.tui.claude_code._markers",
  "smallops.tui.claude_code._parse",
  "smallops.tui.claude_code._gates",
  "smallops.tui.codex._classify",
  "smallops.tui.codex._markers",
  "smallops.tui.codex._parse",
  "smallops.tui.codex._gates",
  "smallops.mux._tmux",
  "smallops.mux._wezterm",
]
```

> The contract rule is the source of truth: **agp may import only from `smallops` public modules (`smallops`, `smallops.mux`, `smallops.tui`); never from any `_*` leaf or `tui.<x>._*` submodule.** The config above implements it via import-linter's `forbidden` contract. Verify the exact syntax against your installed import-linter version with `lint-imports`; if your version supports wildcards, `forbidden_modules = ["smallops._*", "smallops.**._*"]` is cleaner. (`tach` with `tach.toml` dependency rules is an acceptable alternative — pick one, don't run both.)

Wire it into the build so it actually gates:
- Add to the `Makefile` `lint` target (currently `ruff check ...` at `Makefile:638`): append `&& lint-imports`.
- Add `lint-imports` (or `make lint`) to CI so a regression fails the build.

---

## 6. Acceptance Criteria

1. ✅ The two greps in §5.2 return **zero** matches across `src/agp/`.
2. ✅ `ends_with_prompt` and `is_shell_returned` are reachable through smallops's **public** API (Protocol method or exported symbol), and `strip_ansi`, `Mux`, `Tui` are in `smallops.__all__`.
3. ✅ `lint-imports` passes; it is wired into `make lint` and CI.
4. ✅ **Negative test passes:** temporarily add `from smallops._types import SessionInfo` to any `src/agp/` file, run `make lint`, confirm it **fails** with a contract violation, then revert. (Proves the guard actually catches regressions.)
5. ✅ All existing tests still pass: `make test-smallops` (offline parser tests), `make test` (agp suite), and `make lint` (ruff).

---

## 7. Verification Plan

```bash
# 1. Boundary is closed (must be empty)
grep -rnE 'from smallops\._|import smallops\._|smallops\.tui\.[a-z_]+\._' src/agp/

# 2. Lint (ruff + new import contract)
make lint

# 3. smallops parser/property tests unchanged
make test-smallops

# 4. Full agp suite collects and passes (proves no import regressions)
make test

# 5. Prove the guard works (negative test — see AC #4)
```

---

## 8. Risks & Notes

- **Behavior preservation:** This is an import-path + visibility refactor. Do not alter parser/classifier logic. Run `make test-smallops` to confirm parser property tests are byte-for-byte unchanged in behavior.
- **`_classify` as protocol methods:** if you choose the method approach (§5.1, recommended), the `Tui` Protocol (`src/smallops/_protocols.py`) gains two methods; both `ClaudeCodeTui` and `CodexTui` must implement them (delegating to the existing private functions). Keep the private functions as the implementation; the methods are thin public wrappers.
- **Secondary coupling (optional follow-up, NOT required by this PRD):** `ClaudeCodeAdapter._get_or_create_session` hard-requires `isinstance(host, SmallopsTerminalHost)` and raises `TypeError` otherwise. This is *not* a blast-radius source (it doesn't break on smallops renames) — leave it alone in this PR. File it as a separate cleanup if desired.
- **`smallops_tests/` should not need changes** — it tests smallops internals directly and is allowed to import `_*` modules; the contract scopes `source_modules = ["agp"]` only.

---

## 9. Explicitly deferred (do not do in this PR)

- **uv-workspace split** (smallops gets its own `pyproject.toml`, stays in this repo) — the next escalation *if* coupling pain persists after this boundary is enforced. Not needed now.
- **Separate repo / PyPI release** for smallops — only if a second consumer or OSS intent materializes. Not justified by the current goal (reducing blast radius).

Rationale for deferral: the felt "blast radius" traces to the private-import leak (§2), not to repo topology. Closing the leak and enforcing the contract delivers the isolation the team wants at a fraction of the cost of a packaging split, with no new release process and no loss of monorepo atomic-refactor convenience.

---

## Appendix: smallops internal layout (for reference)

```
src/smallops/
  __init__.py        ← public API: Session + __all__ (THE surface agp may use)
  _types.py          ← private: dataclasses/enums (re-exported via __init__)
  _protocols.py      ← private: Mux, Tui Protocols (re-exported)
  _util.py           ← private: strip_ansi, via-file helpers (partly re-exported)
  _poll.py           ← private: polling loop
  mux/__init__.py    ← public: TmuxMux, WezTermMux
  mux/_tmux.py, _wezterm.py   ← private implementations
  tui/__init__.py    ← public: ClaudeCodeTui, CodexTui
  tui/claude_code/   ← _parse, _classify, _gates, _markers (all private leaves)
  tui/codex/         ← same structure
```
