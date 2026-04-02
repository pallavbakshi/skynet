# Backlog

## Sticky mode on tmux — follow-up items

These items were identified during the sticky session mode fix (2026-04-02).
The core fix is in place and tested live for Claude Code on tmux.

### 1. Codex sticky on tmux: no runtime guard for send-keys reliability

**Context:** The codebase previously avoided `send_text` for Codex on tmux,
using per-job `launch_command("codex {prompt}")` instead. A code comment
(`codex.py`, formerly at line 336) noted that "interactive Codex launched
inside tmux accepts the prompt visually but does not progress when the prompt
is injected later with send-keys" — observed on a Linux host.

**Current state:** The sticky fix routes Codex sticky on tmux through
`send_text` to a persistent TUI (same as wezterm). This was verified working
on macOS with codex-cli 0.117.0. However, there is no runtime guard that
detects when `send_text` silently fails (prompt pasted but never executed).

**What to do:**
- Test Codex sticky on tmux on Linux to verify the original issue is resolved
  (it may have been a codex-cli version-specific bug).
- If the issue persists on Linux, add a post-dispatch check: after
  `send_text`, poll the screen for evidence that the TUI accepted the input
  (e.g., the prompt text appears in a `›` turn). If not detected within a
  grace window, fall back to `launch_command` one-shot mode and log a warning.

### 2. Unit test gap: fresh-bootstrap sticky path on tmux

**Context:** The updated unit tests pre-seed `claude_code_bootstrapped` /
`codex_bootstrapped` in session metadata, so they only exercise the
"already-bootstrapped, skip launch" path. The fresh-bootstrap path (empty
session → `is_foreground_tui` returns False → `launch_command` → poll for
ready → set flag) is only covered by live integration tests run manually.

**What to do:**
- Add a unit test with a `TmuxLikeHost` that implements `is_foreground_tui`
  (returning False initially, then True after launch) and verifies that
  sticky mode on tmux:
  1. Does NOT call `reset_session`
  2. DOES call `launch_command` on the first job
  3. Detects the TUI as ready and sets the bootstrap flag
  4. On the second job, skips `launch_command` (flag is set, TUI is alive)
- This should be a deterministic unit test, not a live tmux test.
