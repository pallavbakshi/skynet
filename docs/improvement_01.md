# Improvement 01 — smallops observe methods must not mutate the pane

**Status:** Open · **Filed:** 2026-08-02 · **Package:** `src/smallops`
**Effort:** Small (~3-line core change + tests) · **Risk:** Low

---

## TL;DR

`Session.peek()`, `read()`, `meta()`, and `is_alive()` look like passive
queries but silently **type into the terminal pane** — they auto-dismiss agent
"gates" (permission/trust/OAuth prompts) as a side effect of reading the screen.

This happens because the shared screen-reader helper `_read_screen()` bundles a
call to `_handle_gate()`. That bundling is **redundant**: the methods that
actually drive the agent (`send`, `wait`, `up`) already dismiss gates explicitly
in their polling loops. So the ambient call does no useful work for the driving
methods and only leaks an unwanted side effect into the observe methods.

**Fix:** remove the `_handle_gate(...)` call from `_read_screen()` so it becomes
a pure query. The driving loops keep working unchanged (they already call
`_handle_gate` themselves). Net result: observe methods become side-effect-free,
and a latent double-dismiss bug disappears.

---

## Background (read this if you're new to smallops)

**smallops** drives an interactive TUI agent (Claude Code, Codex) running inside
a terminal pane. A **`Session`** is the controller object — a `Mux` (tmux or
WezTerm, which owns the pane and raw keystroke I/O) composed with a **`Tui`**
(`ClaudeCodeTui` / `CodexTui`, which knows the agent's launch command and how to
interpret its screen). Nothing runs until `Session.up()` is called.

The agent sometimes displays a **gate** — a blocking prompt that stops progress
until answered: first-run setup ("choose the text style"), workspace trust
("yes, i trust this folder"), tool permission requests ("allow bash (y/n)"),
login continuation ("press enter to continue"), the feedback survey, etc. OAuth
login is a **fatal gate** (cannot be auto-dismissed; requires a human/browser).

smallops auto-dismisses the dismissible gates by sending their answer
(e.g. `"y"`, or just Enter) into the pane. Gate patterns live in
`src/smallops/tui/claude_code/_gates.py`.

The `Session` API splits into two families:

| Family | Methods | Expected to mutate? |
|---|---|---|
| **Drive** (advance the agent) | `up`, `send`, `nudge`, `interrupt`, `wait`, `reset`, `down` | **Yes** — they push the agent forward |
| **Observe** (read state/output) | `peek`, `read`, `meta`, `is_alive` | **No** — they should be pure queries |

The bug: the observe family mutates too.

---

## The problem

All four observe methods route through `_read_screen()`, which dismisses gates:

```python
# src/smallops/__init__.py:89-95
def _read_screen(self, n: int | None = None) -> str:
    """Read screen, normalize, handle gates. Returns screen text."""
    session = self._require_session()
    screen = normalize_screen(strip_ansi(self.mux.peek(session, n)))
    _handle_gate(self.mux, self.tui, session, screen)   # <-- SIDE EFFECT: types into pane
    return screen
```

`_handle_gate` (in `_poll.py:38-51`) calls `mux.send_text(pane, response,
enter=True)` when it recognizes a gate. So every `peek()` / `read()` / `meta()`
/ `is_alive()` call can send keystrokes into the agent.

A truly side-effect-free read primitive already exists one layer down —
`self.mux.peek(session, n)` — it just isn't surfaced directly.

---

## Root cause: the ambient call is redundant

Every **driving** loop reads the screen via `_read_screen()` *and then* calls
`_handle_gate()` again, explicitly, using the return value for bookkeeping
(gate counts, deadline resets, re-poll). The ambient call inside `_read_screen`
is therefore fired but its return value is thrown away:

```python
# src/smallops/_poll.py  — same shape in all three loops:
#   wait_for_ready   (lines 68-75)
#   poll_until_done  (lines 128-136)   ← used by send()
#   wait_for_idle    (lines 199-207)   ← used by wait()

        screen = session._read_screen()                       # ambient dismiss fires here

        if _handle_gate(session.mux, session.tui,             # explicit, on the SAME screen
                        session._session, screen):
            gate_count += 1
            if gate_count > config.max_gate_dismissals:
                raise FatalGate("too many gate dismissals")
            unchanged = 0
            deadline = monotonic() + effective_timeout
            continue
```

The driving paths are **self-sufficient**: remove the ambient call and they keep
dismissing gates exactly as before. The ambient call only changes the behavior of
the observe methods — which is precisely the behavior we want to remove.

The intent is documented at the top of `_poll.py` (lines 3-5):

> *"All gate handling is ambient — every screen read checks for gates and
> auto-dismisses them regardless of which operation is running."*

That goal ("never miss a gate, no matter the caller") is reasonable. The flaw is
the **mechanism**: achieving it by hiding mutation inside the read path, instead
of relying on the driving loops (which already handle gates explicitly). Keep
the goal, fix the mechanism.

---

## Concrete harms

1. **Command-query separation violation.** `peek` / `read` / `is_alive` read as
   passive queries but mutate the pane. Callers can't reason about them as reads.
   A monitoring loop that polls `is_alive()` every few seconds will silently keep
   the agent un-wedged — and may auto-answer prompts the operator wanted to see.

2. **Incoherent state reporting.** `meta()` classifies the idle reason from the
   screen *after* `_read_screen` has already dismissed a gate — but from the same
   stale `screen` value that still shows the gate (`__init__.py:~284`). So
   `meta()` can return `idle_reason == GATE` for a gate it has already dismissed.
   The reported state does not match the pane's actual state.

3. **Latent double-dismiss bug.** The ambient call and the explicit loop call
   operate on the *same* stale `screen`. When a gate is present during a polling
   iteration, the gate response is sent **twice**. For a "press enter" gate that
   is harmless (extra newline). For a `(y/n)` gate, the second `y` + Enter lands
   in the agent's prompt buffer and is submitted as a user message. This is
   deterministic given a gate mid-iteration, not a timing race — the practical
   impact varies by gate type and should be pinned down with a test (see below).

---

## The fix

Make `_read_screen()` a pure query by dropping the gate-handling line:

```python
# src/smallops/__init__.py:89-95  (AFTER)
def _read_screen(self, n: int | None = None) -> str:
    """Read and normalize the screen. Pure query — does not mutate the pane."""
    session = self._require_session()
    return normalize_screen(strip_ansi(self.mux.peek(session, n)))
```

That is the entire core change. Consequences:

- The driving loops (`wait_for_ready`, `poll_until_done`, `wait_for_idle`) are
  **unchanged and still dismiss gates** — they call `_handle_gate` explicitly.
- `peek` / `read` / `meta` / `is_alive` become side-effect-free.
- The double-dismiss disappears (one `_handle_gate` per iteration now).
- `meta()`'s `idle_reason == GATE` becomes truthful (it reports a gate that is
  genuinely still on screen, for the caller to act on).

**Files touched:** only `src/smallops/__init__.py` (one line removed, plus update
the docstring). `_poll.py` needs no change.

---

## Verification

**Existing coverage is thin here — note the gap.** There is **no dedicated
smallops unit-test directory**. The relevant behavior is only exercised
indirectly through the agp adapter at `tests/plugins/claude_code/` (see
`test_via_file.py`) and the smallops gate corpus at
`smallops_tests/claude_code/corpus/gates/`. So this fix should add direct unit
coverage for `smallops.Session` observe methods.

Run before and after the change:
```
pytest smallops_tests/ -m offline      # gate-dismissal behavior for the driving path must stay green
pytest tests/                          # full sweep
```

Tests to add (deterministic, no real tmux/wezterm — use a fake `Mux` that
records `send_text` calls):

1. **Observe methods don't mutate.** With a gate visible on the fake screen,
   assert that `peek()`, `read()`, `meta()`, and `is_alive()` make **zero**
   `mux.send_text` calls. (This is the regression this fix introduces on
   purpose — pin it.)
2. **Driving methods still dismiss.** `send()` and `wait()` against a fake screen
   that shows a dismissible gate must still call `_handle_gate` (i.e. send the
   gate answer) and make progress. Confirms no resilience was lost.
3. **Double-dismiss regression.** Construct a fake screen with a `(y/n)` gate and
   drive one `send()` iteration; assert the gate answer is sent **exactly once**
   per gate occurrence. This codifies harm #3 as a test so it can't return.

A fake `Mux` for these tests needs: `peek` (return canned screens from a script),
`send_text` (record calls), `session_exists` (True), `create_session` /
`respawn` / `interrupt` / `destroy_session` (no-ops). Mirror the `Tui` with the
real `ClaudeCodeTui` against the canned screens.

---

## Risk, scope, and behavior change

- **Behavior change (intended):** observe methods no longer auto-dismiss gates.
  Any caller that *relied* on `peek()`/`meta()` silently clearing a gate will now
  see the gate persist until a driving method (`send`/`wait`) handles it. Within
  `src/`, the driving methods always follow observation in practice, so impact is
  expected to be nil — but grep for direct observe-method callers to be sure:
  `grep -rn "\.peek(\|\.read(\|\.meta()\|\.is_alive()" src/`.
- **Risk:** Low. The driving paths are untouched and self-sufficient. The only
  semantic change is the removal of an unintended side effect.
- **Out of scope:** the `agp` adapter (`src/agp/plugins/claude_code/adapter.py`)
  drives smallops via `send()` and does not rely on observe-method gate
  dismissal, so it needs no change. Confirm during verification.

---

## Open decisions (pick before implementing)

1. **Do nothing extra, or add an explicit opt-in?** After the fix, no caller gets
   ambient auto-advance from reads. If some caller genuinely wants "advance past
   trivial gates on observe," expose it as an **explicit, named** action — e.g. a
   `Session.dismiss_gate()` method, or `peek(..., autoskip_gates=False)` defaulting
   off — rather than smuggling it into reads. Recommendation: ship the pure-query
   fix first; add the opt-in only if a concrete caller needs it.
2. **Rename `_read_screen`?** Once it's pure, the name is fine (it does read the
   screen). The docstring is the thing to fix. No rename needed.

---

## Reference index

| What | Where |
|---|---|
| `_read_screen` (the helper to fix) | `src/smallops/__init__.py:89-95` |
| Observe methods that inherit the side effect | `src/smallops/__init__.py` — `peek` (~202), `read` (~206), `is_alive` (~235), `meta` (~254) |
| `_handle_gate` (sends keystrokes) | `src/smallops/_poll.py:38-51` |
| Ambient-intent docstring | `src/smallops/_poll.py:3-5` |
| Explicit gate handling in driving loops | `src/smallops/_poll.py:68-75`, `128-136`, `199-207` |
| Gate pattern tables (what gets auto-dismissed) | `src/smallops/tui/claude_code/_gates.py` |
| `max_gate_dismissals` config (default 10) | `src/smallops/_types.py` (`Config`) |
| Closest existing tests (agp adapter layer) | `tests/plugins/claude_code/test_via_file.py`, `smallops_tests/claude_code/corpus/gates/` |
