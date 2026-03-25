#!/usr/bin/env python3
"""Container E2E test: tmux + Claude Code adapter.

Run inside the agp-runtime Docker image with ANTHROPIC_API_KEY set.
Verifies that Claude Code bootstraps, accepts a prompt, and returns
a cleaned response — all via the TmuxHost + ClaudeCodeAdapter combo.
"""
from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from time import sleep

# Ensure the agp package is importable.
sys.path.insert(0, "/app/src")

from agp.plugins.tmux import TmuxHost
from agp.plugins.claude_code import ClaudeCodeAdapter, _clean_claude_code_output, _strip_ansi


def _ensure_tmux_server() -> None:
    """Start the tmux server if not already running."""
    result = subprocess.run(
        ["tmux", "list-sessions"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print("[setup] no tmux server yet — it will start on first session creation")


def _complete_first_run_setup() -> None:
    """Run a quick -p call to complete Claude Code's first-run setup.

    Interactive TUI shows onboarding screens (theme, login) even when
    credentials are present.  A single -p call with --dangerously-skip-permissions
    completes the first-run setup so subsequent TUI launches skip straight
    to the prompt.
    """
    print("[setup] completing Claude Code first-run setup via -p call...")
    result = subprocess.run(
        ["claude", "--dangerously-skip-permissions", "-p", "echo ok"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode == 0:
        print(f"[setup] first-run setup complete: {result.stdout.strip()[:80]}")
    else:
        print(f"[setup] first-run setup returned {result.returncode}: {(result.stderr or result.stdout or '').strip()[:200]}")


def _run_test() -> bool:
    checkpoint_dir = Path(mkdtemp(prefix="agp-docker-test-"))
    host = TmuxHost(
        scrollback_lines=2000,
        checkpoint_dir=checkpoint_dir,
        default_cwd="/tmp",
    )
    adapter = ClaudeCodeAdapter(
        cli_command="claude",
        idle_poll_seconds=2.0,
        idle_after=3,
        idle_timeout_seconds=120.0,
        session_mode="ephemeral",
        bootstrap_settle_seconds=1.0,
    )

    print("[test] creating tmux session...")
    session = host.get_or_create_session(agent_id="docker-test", workspace_ref="/tmp")
    print(f"[test] session: {session.session_id}")

    print("[test] bootstrapping Claude Code...")
    try:
        adapter.ensure_bootstrapped(host=host, session=session, claimed={})
    except Exception as exc:
        print(f"[FAIL] bootstrap failed: {exc}")
        _dump_screen(host, session)
        return False

    print("[test] Claude Code bootstrapped! Reading visible screen...")
    screen = host.read_visible(session)
    print(f"[test] screen (first 500 chars):\n{_strip_ansi(screen)[:500]}")

    # Send a simple math prompt
    prompt = "What is 7 * 13? Reply with just the number."
    print(f"\n[test] sending prompt: {prompt!r}")

    cursor = host.create_cursor(session)
    host.send_text(session, prompt, enter=True)

    # Poll for completion
    print("[test] waiting for response...")
    from time import monotonic
    deadline = monotonic() + 120.0
    prev = ""
    unchanged = 0
    was_busy = False

    while monotonic() < deadline:
        sleep(2.0)
        screen = _strip_ansi(host.read_visible(session))
        if screen == prev:
            unchanged += 1
            if was_busy and unchanged >= 3:
                print("[test] idle detected after activity — response complete")
                break
        else:
            unchanged = 0
            was_busy = True
        prev = screen
    else:
        print("[WARN] timeout waiting for idle")

    # Read and clean output
    raw_output = _strip_ansi(host.read_visible(session))
    print(f"\n[test] raw visible screen:\n{raw_output}")

    cleaned = _clean_claude_code_output(raw_output)
    print(f"\n[test] cleaned output:\n{cleaned}")

    # Check for expected answer
    if "91" in cleaned:
        print("\n[PASS] correct answer '91' found in output")
        success = True
    else:
        print(f"\n[FAIL] expected '91' in output, got: {cleaned!r}")
        success = False

    # Cleanup
    print("[test] terminating session...")
    host.terminate_session(session)
    return success


def _dump_screen(host: TmuxHost, session) -> None:
    try:
        screen = host.read_visible(session)
        print(f"[debug] visible screen:\n{_strip_ansi(screen)}")
    except Exception:
        print("[debug] could not read screen")


if __name__ == "__main__":
    _ensure_tmux_server()
    _complete_first_run_setup()
    ok = _run_test()
    sys.exit(0 if ok else 1)
