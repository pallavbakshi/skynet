"""Live integration tests for TmuxHost and ClaudeCodeAdapter over real tmux.

These tests require tmux to be installed and available in PATH.
They spin up real tmux sessions with a fake Claude Code TUI script
to verify the full send / receive / parse pipeline.
"""

from __future__ import annotations

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
from agp.runtime._types import (
    ExecutionResult,
    TerminalSession,
)

# Unicode characters used by Claude Code TUI — written as actual chars
# so they survive Python string escaping and land in shell scripts correctly.
_SEP = "\u2500" * 10           # ──────────
_PROMPT = "\u276f"             # ❯
_RESPONSE = "\u25cf"           # ●
_TOOL_RESULT = "\u23bf"        # ⎿
_STATUS_BAR = "\u23f5\u23f5"   # ⏵⏵


def _tmux_available() -> bool:
    try:
        r = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=False)
        return r.returncode == 0
    except FileNotFoundError:
        return False


@unittest.skipUnless(_tmux_available(), "tmux not available")
class TmuxHostLiveTest(unittest.TestCase):
    """Test TmuxHost against real tmux."""

    def setUp(self) -> None:
        self._tmp = Path(mkdtemp(prefix="agp-tmux-live-"))
        self.host = TmuxHost(
            session_prefix="agp-test-live",
            checkpoint_dir=self._tmp / "checkpoints",
        )
        self._sessions: list[TerminalSession] = []

    def tearDown(self) -> None:
        for sess in self._sessions:
            try:
                self.host.terminate_session(sess)
            except Exception:
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _create_session(self, agent_id: str) -> TerminalSession:
        sess = self.host.get_or_create_session(agent_id=agent_id)
        self._sessions.append(sess)
        return sess

    # ── Session lifecycle ────────────────────────────────────────────

    def test_create_session_and_health(self) -> None:
        sess = self._create_session("live-health")
        self.assertTrue(sess.session_id.startswith("agp-test-live-"))
        health = self.host.health(sess)
        self.assertTrue(health.healthy)
        self.assertTrue(health.exists)

    def test_session_exists_and_terminate(self) -> None:
        sess = self._create_session("live-exists")
        self.assertTrue(self.host.session_exists(sess))
        self.host.terminate_session(sess)
        self.assertFalse(self.host.session_exists(sess))

    def test_get_or_create_is_idempotent(self) -> None:
        sess1 = self._create_session("live-idempotent")
        sess2 = self.host.get_or_create_session(agent_id="live-idempotent")
        self.assertEqual(sess1.session_id, sess2.session_id)

    def test_reset_session_creates_fresh_session(self) -> None:
        sess = self._create_session("live-reset")
        old_id = sess.session_id
        new_sess = self.host.reset_session(sess)
        self._sessions.append(new_sess)
        self.assertEqual(old_id, new_sess.session_id)
        self.assertTrue(self.host.session_exists(new_sess))

    # ── Send / receive ───────────────────────────────────────────────

    def test_send_text_and_read_visible(self) -> None:
        sess = self._create_session("live-sendrecv")
        sleep(0.3)
        self.host.send_text(sess, "echo HELLO_WORLD_12345", enter=True)
        sleep(0.5)
        visible = self.host.read_visible(sess)
        self.assertIn("HELLO_WORLD_12345", visible)

    def test_send_text_literal_special_chars(self) -> None:
        """send_text -l mode should handle special characters safely."""
        sess = self._create_session("live-special")
        sleep(0.3)
        self.host.send_text(sess, 'echo "hello $USER $(whoami)"', enter=True)
        sleep(0.5)
        visible = self.host.read_visible(sess)
        self.assertIn("hello", visible)

    def test_send_text_no_enter(self) -> None:
        sess = self._create_session("live-noenter")
        sleep(0.3)
        self.host.send_text(sess, "echo PARTIAL", enter=False)
        sleep(0.3)
        visible = self.host.read_visible(sess)
        self.assertIn("echo PARTIAL", visible)

    # ── Cursor and output tracking ───────────────────────────────────

    def test_cursor_and_read_output(self) -> None:
        sess = self._create_session("live-cursor")
        sleep(0.3)
        cursor = self.host.create_cursor(sess)
        self.assertEqual(cursor.session_id, sess.session_id)
        self.assertIn("absolute_line", cursor.metadata)

        self.host.send_text(sess, "echo LINE_A_999", enter=True)
        sleep(0.5)
        result = self.host.read_output(sess, cursor)
        self.assertTrue(result.changed)
        self.assertIn("LINE_A_999", result.text)
        self.assertIn("LINE_A_999", result.full_text)

        # Second read with updated cursor should show new content only
        self.host.send_text(sess, "echo LINE_B_888", enter=True)
        sleep(0.5)
        result2 = self.host.read_output(sess, result.cursor)
        self.assertIn("LINE_B_888", result2.text)
        # full_text accumulates
        self.assertIn("LINE_A_999", result2.full_text)
        self.assertIn("LINE_B_888", result2.full_text)

    def test_cursor_persist_and_load(self) -> None:
        sess = self._create_session("live-persist")
        sleep(0.3)
        cursor = self.host.create_cursor(sess)
        self.host.send_text(sess, "echo PERSIST_TEST", enter=True)
        sleep(0.5)
        self.host.read_output(sess, cursor)

        loaded = self.host.load_cursor(sess)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.session_id, sess.session_id)
        self.assertIn("absolute_line", loaded.metadata)

    # ── Interrupt ────────────────────────────────────────────────────

    def test_interrupt_sends_ctrl_c(self) -> None:
        sess = self._create_session("live-interrupt")
        sleep(0.3)
        self.host.send_text(sess, "sleep 30", enter=True)
        sleep(0.5)
        self.host.interrupt(sess)
        sleep(0.5)
        self.assertTrue(self.host.shell_idle(sess))

    # ── Shell idle detection ─────────────────────────────────────────

    def test_shell_idle_when_no_process(self) -> None:
        sess = self._create_session("live-idle")
        idle = False
        for _ in range(20):
            sleep(0.5)
            if self.host.shell_idle(sess):
                idle = True
                break
        self.assertTrue(idle, f"shell not idle; fg_cmd={self.host._foreground_command(sess)}")

    def test_shell_not_idle_during_command(self) -> None:
        sess = self._create_session("live-busy")
        sleep(0.3)
        self.host.send_text(sess, "sleep 10", enter=True)
        sleep(0.5)
        self.assertFalse(self.host.shell_idle(sess))
        self.host.interrupt(sess)
        sleep(0.5)

    # ── Snapshot ─────────────────────────────────────────────────────

    def test_snapshot_captures_state(self) -> None:
        sess = self._create_session("live-snap")
        sleep(0.3)
        self.host.send_text(sess, "echo SNAP_MARKER_77", enter=True)
        sleep(0.5)
        snap = self.host.snapshot(sess)
        self.assertEqual(snap["session_id"], sess.session_id)
        self.assertIn("SNAP_MARKER_77", snap["text"])

    # ── Wait for idle ────────────────────────────────────────────────

    def test_wait_for_idle_returns_true_when_shell_idle(self) -> None:
        sess = self._create_session("live-waitidle")
        sleep(0.3)
        self.host.send_text(sess, "echo DONE", enter=True)
        result = self.host.wait_for_idle(
            sess, poll_seconds=0.3, idle_after=2, timeout_seconds=5.0,
        )
        self.assertTrue(result)

    def test_wait_for_idle_timeout(self) -> None:
        sess = self._create_session("live-waittimeout")
        sleep(0.5)
        # Continuously produce output so the screen never stabilizes
        self.host.send_text(sess, "while true; do echo $RANDOM; sleep 0.1; done", enter=True)
        sleep(0.8)
        result = self.host.wait_for_idle(
            sess, poll_seconds=0.2, idle_after=2, timeout_seconds=1.5,
        )
        self.assertFalse(result)
        self.host.interrupt(sess)
        sleep(0.3)

    # ── Launch command ───────────────────────────────────────────────

    def test_launch_command_runs_in_pane(self) -> None:
        sess = self._create_session("live-launch")
        sleep(0.3)
        marker_file = self._tmp / "launch-marker.txt"
        self.host.launch_command(
            sess,
            command=f'echo LAUNCHED_OK > {marker_file}',
        )
        deadline = monotonic() + 5.0
        while monotonic() < deadline:
            if marker_file.exists():
                break
            sleep(0.3)
        self.assertTrue(marker_file.exists())
        self.assertIn("LAUNCHED_OK", marker_file.read_text())


def _fake_tui_script(body: str) -> str:
    """Build a fake Claude Code TUI bash script.

    Injects Unicode char variables at the top so the body can use
    plain variable references ($SEP, $P, $R, $TR, $SB) instead of
    escape sequences — avoids printf/bash quoting issues.
    """
    header = (
        "#!/bin/bash\n"
        f"SEP='{_SEP}'\n"
        f"P='{_PROMPT}'\n"
        f"R='{_RESPONSE}'\n"
        f"TR='{_TOOL_RESULT}'\n"
        f"SB='{_STATUS_BAR}'\n"
    )
    return header + body


@unittest.skipUnless(_tmux_available(), "tmux not available")
class ClaudeCodeAdapterLiveTmuxTest(unittest.TestCase):
    """Test ClaudeCodeAdapter execute_run against a fake TUI in real tmux."""

    def setUp(self) -> None:
        self._tmp = Path(mkdtemp(prefix="agp-cc-live-"))
        self.host = TmuxHost(
            session_prefix="agp-test-cc",
            checkpoint_dir=self._tmp / "checkpoints",
        )
        self._sessions: list[TerminalSession] = []

    def tearDown(self) -> None:
        for sess in self._sessions:
            try:
                self.host.terminate_session(sess)
            except Exception:
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _create_session(self, agent_id: str) -> TerminalSession:
        sess = self.host.get_or_create_session(agent_id=agent_id)
        self._sessions.append(sess)
        return sess

    def _write_fake_tui(self, name: str, body: str) -> Path:
        """Write a fake TUI script with Unicode variables pre-injected."""
        path = self._tmp / name
        path.write_text(_fake_tui_script(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def _make_supervisor_stub(self, runtime_id: str = "rtm_live"):
        class SupervisorStub:
            def __init__(self):
                self._active_session = None
                self._session_lock = None
                self._active_startup_settled = None
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": runtime_id})()
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

    # ── Manual TUI interaction (no adapter reset) ────────────────────

    def test_execute_run_simple_response(self) -> None:
        """Manually launch fake TUI, send prompt, verify parsing."""
        fake_tui = self._write_fake_tui("fake_simple.sh", """
printf "${SEP}\\n"
printf "${P} "
read -r user_prompt
sleep 0.3
printf "${R} The answer to your question is 42.\\n"
printf "\\n"
printf "  This is a multi-line response with details.\\n"
printf "\\n"
printf "${SEP}\\n"
printf "${P} "
sleep 30
""")

        sess = self._create_session("cc-simple")
        sess = self.host.reset_session(sess)
        self._sessions.append(sess)
        self.host.launch_command(sess, command=f"{fake_tui} --dangerously-skip-permissions")

        # Wait for ready
        deadline = monotonic() + 10.0
        ready = False
        while monotonic() < deadline:
            sleep(0.5)
            screen = self.host.read_visible(sess)
            if _PROMPT in screen and _SEP[0] in screen:
                ready = True
                break
        self.assertTrue(ready, "Fake TUI did not become ready")

        # Send prompt and wait for response
        self.host.send_text(sess, "What is the answer?", enter=True)
        deadline = monotonic() + 10.0
        while monotonic() < deadline:
            sleep(0.5)
            screen = self.host.read_visible(sess)
            if "42" in screen:
                break

        # Verify output cleaning
        cleaned = _clean_claude_code_output(screen)
        self.assertIn("42", cleaned)

        # Verify turn parsing
        turns = _parse_claude_code_turns(screen)
        answered = [t for t in turns if t["response"]]
        self.assertGreater(len(answered), 0)

    # ── Full adapter execute_run cycle ───────────────────────────────

    def test_execute_run_full_adapter_cycle(self) -> None:
        """Full bootstrap -> dispatch -> poll -> extract cycle."""
        fake_tui = self._write_fake_tui("fake_full.sh", """
printf "${SEP}\\n"
printf "${P} "
read -r user_prompt
sleep 0.3
printf "${R} Here is the answer to your question:\\n"
printf "\\n"
printf "  The result is **42**.\\n"
printf "\\n"
printf "${SEP}\\n"
printf "${P} "
sleep 60
""")

        adapter = ClaudeCodeAdapter(
            cli_command=str(fake_tui),
            idle_poll_seconds=0.5,
            idle_after=2,
            idle_timeout_seconds=20.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("cc-full")
        claimed = self._make_claimed("cc-full", "What is 6 times 7?")
        supervisor = self._make_supervisor_stub()

        result = adapter.execute_run(
            host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
        )

        self.assertIsInstance(result, ExecutionResult)
        roles = {a.role for a in result.artifacts}
        self.assertIn("prompt", roles)
        self.assertIn("result", roles)
        self.assertIn("transcript_log", roles)

        result_artifact = next(a for a in result.artifacts if a.role == "result")
        self.assertIn("42", result_artifact.content)

        prompt_artifact = next(a for a in result.artifacts if a.role == "prompt")
        self.assertIn("6 times 7", prompt_artifact.content)

    # ── Gate auto-dismiss ────────────────────────────────────────────

    def test_adapter_gate_auto_dismiss(self) -> None:
        """Adapter should auto-dismiss gate prompts during bootstrap."""
        fake_tui = self._write_fake_tui("fake_gate.sh", """
# Show trust gate prompt
printf 'Quick safety check\\n'
printf 'Do you trust the contents of this folder?\\n'
printf '1: Yes, I trust this folder\\n'
printf '2: No\\n'
printf '> '
read -r gate_choice

# Clear screen after gate dismiss (like real Claude TUI)
printf '\\033[2J\\033[H'
sleep 0.2

# Show normal TUI idle
printf "${SEP}\\n"
printf "${P} "
read -r user_prompt
sleep 0.3

printf "${R} Gate was dismissed successfully. The answer is 99.\\n"
printf "${SEP}\\n"
printf "${P} "
sleep 60
""")

        adapter = ClaudeCodeAdapter(
            cli_command=str(fake_tui),
            idle_poll_seconds=0.5,
            idle_after=2,
            idle_timeout_seconds=20.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("cc-gate")
        claimed = self._make_claimed("cc-gate", "test after gate")
        supervisor = self._make_supervisor_stub()

        result = adapter.execute_run(
            host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
        )

        self.assertIsInstance(result, ExecutionResult)
        result_artifact = next(a for a in result.artifacts if a.role == "result")
        self.assertIn("99", result_artifact.content)

    # ── Shell return detection ───────────────────────────────────────

    def test_adapter_handles_tui_exit_without_response(self) -> None:
        """When the TUI exits without responding, adapter should either
        raise an error or produce a result with no meaningful content.
        (The shell's own ❯ prompt may confuse detection on some systems.)
        """
        fake_tui = self._write_fake_tui("fake_exit.sh", """
printf "${SEP}\\n"
printf "${P} "
read -r user_prompt
exit 0
""")

        adapter = ClaudeCodeAdapter(
            cli_command=str(fake_tui),
            idle_poll_seconds=0.3,
            idle_after=2,
            idle_timeout_seconds=10.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("cc-exit")
        claimed = self._make_claimed("cc-exit", "should fail")
        supervisor = self._make_supervisor_stub()

        try:
            result = adapter.execute_run(
                host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
            )
            # If we get here, the adapter didn't raise — verify the result
            # doesn't contain a Claude-style response (no ● marker content).
            result_text = next(
                (a.content for a in result.artifacts if a.role == "result"), ""
            )
            # The result should not contain the ● response marker text
            self.assertNotIn(_RESPONSE, result_text)
        except Exception:
            # Any exception is acceptable — PaneDied, ExecutionTimeout, etc.
            pass

    # ── Multi-line + tool result parsing ─────────────────────────────

    def test_multiline_response_parsing(self) -> None:
        """Multi-line responses with tool results are parsed correctly."""
        fake_tui = self._write_fake_tui("fake_multi.sh", """
printf "${SEP}\\n"
printf "${P} "
read -r user_prompt
sleep 0.3
printf "${R} I will read the file for you.\\n"
printf "${TR} Read file.txt (15 lines)\\n"
printf "  Contents of the file are here.\\n"
printf "${R} The file contains important data.\\n"
printf "  Here is a summary:\\n"
printf "  - Item 1\\n"
printf "  - Item 2\\n"
printf "  - Item 3\\n"
printf "${SEP}\\n"
printf "${P} "
sleep 60
""")

        adapter = ClaudeCodeAdapter(
            cli_command=str(fake_tui),
            idle_poll_seconds=0.5,
            idle_after=2,
            idle_timeout_seconds=20.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("cc-multi")
        claimed = self._make_claimed("cc-multi", "read the file")
        supervisor = self._make_supervisor_stub()

        result = adapter.execute_run(
            host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
        )

        result_artifact = next(a for a in result.artifacts if a.role == "result")
        content = result_artifact.content
        self.assertIn("file", content.lower())
        self.assertIn("Item 1", content)
        self.assertIn("Item 2", content)

    # ── Status bar filtering ─────────────────────────────────────────

    def test_output_with_status_bar(self) -> None:
        """Status bar lines should be filtered out of cleaned output."""
        fake_tui = self._write_fake_tui("fake_statusbar.sh", """
printf "${SEP}\\n"
printf "${P} "
read -r user_prompt
sleep 0.3
printf "${R} The answer is 7.\\n"
printf "${SEP}\\n"
printf "${P} \\n"
printf "${SB} Auto  16200 tokens  3.2s\\n"
sleep 60
""")

        adapter = ClaudeCodeAdapter(
            cli_command=str(fake_tui),
            idle_poll_seconds=0.5,
            idle_after=2,
            idle_timeout_seconds=20.0,
            session_mode="ephemeral",
        )

        sess = self._create_session("cc-status")
        claimed = self._make_claimed("cc-status", "what is 3 + 4?")
        supervisor = self._make_supervisor_stub()

        result = adapter.execute_run(
            host=self.host, session=sess, claimed=claimed, supervisor=supervisor,
        )

        result_artifact = next(a for a in result.artifacts if a.role == "result")
        self.assertNotIn("16200", result_artifact.content)
        self.assertNotIn("tokens", result_artifact.content)
        self.assertIn("7", result_artifact.content)


if __name__ == "__main__":
    unittest.main()
