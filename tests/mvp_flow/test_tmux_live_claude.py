"""Live integration tests: real Claude Code TUI running in real tmux.

These tests spin up an actual `claude --dangerously-skip-permissions`
process inside a tmux pane and exercise the TmuxHost + ClaudeCodeAdapter
pipeline end-to-end: bootstrap, send prompt, poll for completion,
parse TUI output, extract cleaned result.

Requirements:
  - tmux installed
  - `claude` CLI installed and authenticated (API key available)
  - Network access to Anthropic API

These are slow (~30-60s each) and cost real API tokens.
Run explicitly:  python -m pytest tests/mvp_flow/test_tmux_live_claude.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic, sleep

from agp.plugins.tmux import TmuxHost
from agp.plugins.claude_code import (
    ClaudeCodeAdapter,
    _clean_claude_code_output,
    _parse_claude_code_turns,
)
from agp.runtime import _strip_ansi
from agp.runtime._types import (
    ExecutionResult,
    TerminalSession,
)


def _tmux_available() -> bool:
    try:
        r = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _claude_available() -> bool:
    for path in ["/opt/homebrew/bin/claude", "claude"]:
        try:
            r = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if r.returncode == 0 and "Claude Code" in r.stdout:
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return False


def _claude_bin() -> str:
    """Return the path to the claude binary."""
    for path in ["/opt/homebrew/bin/claude", "claude"]:
        try:
            r = subprocess.run(
                [path, "--version"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if r.returncode == 0:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return "claude"


@unittest.skipUnless(_tmux_available(), "tmux not available")
@unittest.skipUnless(_claude_available(), "claude CLI not available")
class RealClaudeCodeLiveTmuxTest(unittest.TestCase):
    """Drive the real Claude Code TUI through the adapter layer."""

    CLAUDE_BIN = _claude_bin()

    def setUp(self) -> None:
        self._tmp = Path(mkdtemp(prefix="agp-real-claude-"))
        self.host = TmuxHost(
            session_prefix="agp-real-cc",
            checkpoint_dir=self._tmp / "checkpoints",
        )
        self._sessions: list[TerminalSession] = []

    def tearDown(self) -> None:
        for sess in self._sessions:
            try:
                self.host.interrupt(sess)
                sleep(0.5)
                self.host.terminate_session(sess)
            except Exception:
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _create_session(self, agent_id: str) -> TerminalSession:
        sess = self.host.get_or_create_session(agent_id=agent_id)
        self._sessions.append(sess)
        return sess

    def _make_supervisor_stub(self):
        class SupervisorStub:
            def __init__(self):
                self._active_session = None
                self._session_lock = None
                self._active_startup_settled = None
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_live_real"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        return SupervisorStub()

    def _make_claimed(self, agent_id: str, prompt: str) -> dict:
        return {
            "agent_id": agent_id,
            "job": {"job_id": f"job_{agent_id}"},
            "run": {"run_id": f"run_{agent_id}"},
            "message": {"text": prompt},
        }

    # ── Test 1: Bootstrap (launch + ready detection) ─────────────────

    def test_bootstrap_launches_and_detects_ready(self) -> None:
        """ClaudeCodeAdapter.ensure_bootstrapped should launch claude
        and detect the idle ❯ prompt."""
        adapter = ClaudeCodeAdapter(
            cli_command=self.CLAUDE_BIN,
            idle_poll_seconds=1.0,
            idle_after=2,
            idle_timeout_seconds=60.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("real-bootstrap")
        claimed = self._make_claimed("real-bootstrap", "unused")

        # ensure_bootstrapped should launch claude and wait for ❯
        adapter.ensure_bootstrapped(host=self.host, session=sess, claimed=claimed)
        self.assertTrue(sess.metadata.get("claude_code_bootstrapped"))

        # Verify the TUI is actually showing the idle prompt
        screen = _strip_ansi(self.host.read_visible(sess))
        self.assertTrue(
            adapter._looks_like_ready(screen),
            f"TUI not ready after bootstrap. Screen:\n{screen[:300]}",
        )

    # ── Test 2: Full execute_run with a trivial prompt ───────────────

    def test_execute_run_simple_math(self) -> None:
        """Send '2+2=?' to real Claude, verify we get '4' back."""
        adapter = ClaudeCodeAdapter(
            cli_command=self.CLAUDE_BIN,
            idle_poll_seconds=2.0,
            idle_after=3,
            idle_timeout_seconds=120.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("real-math")
        claimed = self._make_claimed(
            "real-math",
            "What is 2+2? Reply with ONLY the number, nothing else.",
        )
        supervisor = self._make_supervisor_stub()

        result = adapter.execute_run(
            host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
        )

        self.assertIsInstance(result, ExecutionResult)

        # Check artifact structure
        roles = {a.role for a in result.artifacts}
        self.assertIn("prompt", roles)
        self.assertIn("result", roles)
        self.assertIn("transcript_log", roles)
        self.assertIn("exec_log", roles)

        # The result should contain "4" somewhere
        result_text = next(a.content for a in result.artifacts if a.role == "result")
        self.assertIn("4", result_text, f"Expected '4' in result: {result_text[:200]}")

        # The prompt artifact should contain the original prompt
        prompt_text = next(a.content for a in result.artifacts if a.role == "prompt")
        self.assertIn("2+2", prompt_text)

        # The transcript should have TUI content (separators, prompts)
        transcript = next(a.content for a in result.artifacts if a.role == "transcript_log")
        self.assertTrue(len(transcript) > 10, "Transcript too short")

    # ── Test 3: Output cleaning on real TUI output ───────────────────

    def test_output_cleaning_on_real_tui(self) -> None:
        """Verify _clean_claude_code_output strips real TUI chrome."""
        adapter = ClaudeCodeAdapter(
            cli_command=self.CLAUDE_BIN,
            idle_poll_seconds=2.0,
            idle_after=3,
            idle_timeout_seconds=120.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("real-clean")
        claimed = self._make_claimed(
            "real-clean",
            'Say exactly "HELLO_WORLD" and nothing else.',
        )
        supervisor = self._make_supervisor_stub()

        result = adapter.execute_run(
            host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
        )

        result_text = next(a.content for a in result.artifacts if a.role == "result")
        transcript = next(a.content for a in result.artifacts if a.role == "transcript_log")

        # The cleaned result should contain the response but not TUI chrome
        self.assertIn("HELLO_WORLD", result_text)
        self.assertNotIn("\u2500\u2500\u2500\u2500", result_text)  # no separator
        self.assertNotIn("\u23f5\u23f5", result_text)  # no status bar

        # The transcript (raw TUI) should have chrome
        self.assertTrue(
            "\u2500" in transcript or "\u276f" in transcript,
            "Transcript should contain TUI markers",
        )

    # ── Test 4: Turn parsing on real output ──────────────────────────

    def test_turn_parsing_on_real_output(self) -> None:
        """Verify _parse_claude_code_turns finds real turns."""
        adapter = ClaudeCodeAdapter(
            cli_command=self.CLAUDE_BIN,
            idle_poll_seconds=2.0,
            idle_after=3,
            idle_timeout_seconds=120.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("real-turns")
        sess = self.host.reset_session(sess)
        self._sessions.append(sess)

        # Bootstrap manually so we can inspect the screen
        adapter.ensure_bootstrapped(
            host=self.host, session=sess,
            claimed=self._make_claimed("real-turns", "unused"),
        )

        # Send a prompt
        self.host.send_text(sess, "What is 3+3? Reply with ONLY the number.", enter=True)

        # Wait for response
        deadline = monotonic() + 60.0
        screen = ""
        while monotonic() < deadline:
            sleep(2.0)
            screen = _strip_ansi(self.host.read_visible(sess))
            if adapter._looks_like_completed_turn(
                screen, baseline_answered_turns=0, baseline_last_response=None,
            ):
                break

        # Parse turns from the real screen
        turns = _parse_claude_code_turns(screen)
        self.assertGreater(len(turns), 0, f"No turns parsed from screen:\n{screen[:500]}")

        answered = [t for t in turns if t["response"]]
        self.assertGreater(len(answered), 0, "No answered turns found")

        # The response should mention "6"
        last_response = "\n".join(answered[-1]["response"])
        self.assertIn("6", last_response, f"Expected '6' in: {last_response[:200]}")

    # ── Test 5: Cursor tracking with real output ─────────────────────

    def test_cursor_tracking_with_real_claude(self) -> None:
        """Verify cursor-based output tracking captures real TUI output.

        Uses read_visible as the primary check since Claude Code runs in
        tmux's alternate screen buffer where history_size=0 makes
        cursor-based read_output unreliable for content detection.
        """
        sess = self._create_session("real-cursor")
        sess = self.host.reset_session(sess)
        self._sessions.append(sess)

        adapter = ClaudeCodeAdapter(
            cli_command=self.CLAUDE_BIN,
            idle_poll_seconds=2.0,
            idle_after=3,
            idle_timeout_seconds=60.0,
            session_mode="ephemeral",
        )
        adapter.ensure_bootstrapped(
            host=self.host, session=sess,
            claimed=self._make_claimed("real-cursor", "unused"),
        )

        # Create cursor before sending prompt
        cursor = self.host.create_cursor(sess)
        self.host.send_text(sess, "Say exactly: CURSOR_TEST_OK", enter=True)

        # Poll until we see the response on the visible screen
        deadline = monotonic() + 60.0
        visible = ""
        while monotonic() < deadline:
            sleep(2.0)
            visible = _strip_ansi(self.host.read_visible(sess))
            # Also advance cursor so accumulator stays current
            read_result = self.host.read_output(sess, cursor)
            cursor = read_result.cursor
            if "CURSOR_TEST_OK" in visible:
                break

        self.assertIn("CURSOR_TEST_OK", visible,
                       f"Response not on screen. Got: {visible[:300]}")
        # Verify cursor machinery didn't crash — the cursor should have advanced
        self.assertGreater(cursor.metadata.get("absolute_line", 0), 0)

    # ── Test 6: inspect_output on real screen ────────────────────────

    def test_inspect_output_on_real_screen(self) -> None:
        """Verify adapter.inspect_output produces correct classification."""
        adapter = ClaudeCodeAdapter(
            cli_command=self.CLAUDE_BIN,
            idle_poll_seconds=2.0,
            idle_after=3,
            idle_timeout_seconds=120.0,
        )

        sess = self._create_session("real-inspect")
        claimed = self._make_claimed(
            "real-inspect",
            "What is 5+5? Reply with ONLY the number.",
        )
        supervisor = self._make_supervisor_stub()

        result = adapter.execute_run(
            host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
        )

        # Use inspect_output on the transcript
        transcript = next(a.content for a in result.artifacts if a.role == "transcript_log")
        inspection = adapter.inspect_output(text=transcript, run_id="run_real_inspect")

        self.assertTrue(inspection["supported"])
        self.assertEqual(inspection["adapter_kind"], "claude_code")
        self.assertEqual(inspection["mode"], "tui")
        self.assertIn("cleaned_output", inspection)
        self.assertIn("10", inspection["cleaned_output"])


if __name__ == "__main__":
    unittest.main()
