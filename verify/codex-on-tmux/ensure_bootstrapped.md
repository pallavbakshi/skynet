# Manual Verification: `ensure_bootstrapped` (Codex on tmux)

## What this tests

The `CodexAdapter.ensure_bootstrapped()` flow:
1. Creates a tmux session
2. Launches `codex` CLI inside it
3. Polls the screen until the Codex prompt (`›`) is visible AND stable
4. Auto-dismisses any gate prompts that appear during startup
5. Raises `AuthFailure` immediately on fatal gates (usage limits, login required)
6. Sets `codex_bootstrapped: True` in session metadata

## Prerequisites

- `codex` CLI installed and in PATH (`which codex`)
- `tmux` installed
- Valid API key or auth configured for codex (not rate-limited)
- No existing `agp-manual-test` tmux session

## Cleanup (before and after)

```bash
skyops host terminate tmux agp-manual-test manual-test
```

## 1. Happy path: fresh bootstrap

```bash
AGP_CODEX_TUI_MODE=true skyops adapter bootstrap codex tmux manual-test \
  --workspace-ref /Users/pb/projects/skynet
```

**Expected output**:
```json
{
  "adapter_kind": "codex",
  "agent_id": "manual-test",
  "host_kind": "tmux",
  "metadata": {
    "codex_bootstrapped": true,
    "tmux_session": "agp-manual-test"
  },
  "session_id": "agp-manual-test"
}
```

**Verify**: `codex_bootstrapped: true`. Takes ~8-12s (stability window: 3 polls x 2s).

Optionally watch live in a separate terminal:
```bash
tmux attach -t agp-manual-test
```

## 2. Health check after bootstrap

```bash
skyops host health tmux agp-manual-test manual-test
```

**Expected**: `"healthy": true`.

## 3. Read screen after bootstrap

```bash
skyops host read tmux agp-manual-test manual-test
```

**Expected**: `visible_text` shows codex idle prompt (`›`), no error gates.

## 4. Idempotency: second bootstrap is instant

Running `adapter bootstrap` again on the same session should return immediately (skips bootstrap because `codex_bootstrapped` is already set in the in-memory session metadata).

Note: the CLI commands reconstruct session objects fresh each invocation, so the metadata flag only persists within a single `StandalonePluginRunner` or `run-once --keep-session` chain. To test idempotency, use `plugin repl` or sequential `run-once --keep-session` calls.

## 5. Fatal gate handling

If codex hits a usage limit during bootstrap or execution, the adapter raises `AuthFailure` immediately instead of spinning until timeout.

**During execution** (`run-once`):
```bash
AGP_CODEX_TUI_MODE=true skyops adapter run-once codex tmux manual-test \
  --task "test task" --keep-session
```

If rate-limited, output includes:
```json
{
  "error": "codex hit a fatal gate that cannot be auto-dismissed ...",
  "exception_type": "AuthFailure",
  "ok": false
}
```

Progress events show `"tui_state": "gate.fatal"` on first poll.

**During bootstrap**: same `AuthFailure` raised if codex shows a usage-limit screen before reaching the prompt.

## 6. Gate types

**Auto-dismissed** (sends keystroke + Enter during bootstrap):
- "trust the contents" / "do you trust"
- "press enter to continue"
- "approaching rate limits" → sends `3` (keep current model)
- "introducing gpt-..." / "try new model" → sends `2` (use existing)
- Welcome/onboarding → sends `1`, `2`, or `3` per env vars

**Fatal** (raises `AuthFailure`):
- "you've hit your usage limit"
- "upgrade to pro" / "purchase more credits"
- Welcome/onboarding (sign in required)

## 7. Stability requirement

Bootstrap requires `max(idle_after, 2)` consecutive identical screens before declaring ready. With defaults (`idle_after=3`, `idle_poll_seconds=2.0`), the screen must be unchanged for 6 seconds (3 polls x 2s). This prevents false-ready on screens still rendering (update banners, model info).

## Key source files

- `src/agp/plugins/codex/adapter.py` — `ensure_bootstrapped()` (lines 130-198)
- `src/agp/plugins/codex/_gates.py` — gate detection and auto-response
- `src/agp/plugins/codex/_classify.py` — `looks_like_codex_ready()`
- `src/agp/plugins/tmux.py` — `TmuxHost`, `launch_command()`, `read_visible()`
- `src/agp/_plugin_cli.py` — CLI commands (`adapter bootstrap`, `run-once`, etc.)
- `src/agp/runtime/_standalone.py` — `StandalonePluginRunner`
