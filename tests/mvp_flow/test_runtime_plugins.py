"""Runtime host, adapter, plugin, and terminal integration flows."""

from tests.mvp_flow.base import *


class MvpFlowRuntimePluginsTest(MvpFlowTestBase):
    def test_codex_adapter_health_check_detects_lost_session(self) -> None:
        call_count = {"n": 0}

        class DisappearingHost(InProcessTerminalHost):
            def health(self, session):
                call_count["n"] += 1
                if call_count["n"] >= 3:
                    from agp.runtime import SessionHealth
                    return SessionHealth(
                        session_id=session.session_id,
                        exists=False,
                        healthy=False,
                        reason="pane_vanished",
                    )
                return super().health(session)

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_disappear"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(max_polls=50, poll_interval_seconds=0.0, health_check_interval_polls=2)
        host = DisappearingHost()
        session = host.get_or_create_session(agent_id="agt_disappear")
        claimed = {
            "agent_id": "agt_disappear",
            "job": {"job_id": "job_disappear"},
            "run": {"run_id": "run_disappear"},
            "message": {"text": "vanishing work"},
        }
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("session lost", str(ctx.exception))

    def test_codex_adapter_bootstrap_verifies_session_health(self) -> None:
        from agp.runtime import SessionHealth

        class UnhealthyHost(InProcessTerminalHost):
            def health(self, session):
                return SessionHealth(
                    session_id=session.session_id,
                    exists=False,
                    healthy=False,
                    reason="pane_dead",
                )

        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)
        host = UnhealthyHost()
        session = host.get_or_create_session(agent_id="agt_boot_health")
        claimed = {
            "agent_id": "agt_boot_health",
            "job": {"job_id": "job_boot_health"},
            "run": {"run_id": "run_boot_health"},
            "message": {"text": "boot check"},
        }
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        self.assertIn("unhealthy before bootstrap", str(ctx.exception))

    def test_codex_adapter_invalid_status_triggers_recovery(self) -> None:
        class CodexHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("AGP_RUN_BEGIN run_badstatus"):
                    self._history.setdefault(session.session_id, []).append(
                        'AGP_RUN_RESULT run_badstatus {"status":"unknown_state","result":"huh"}\n'
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_bs"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)
        host = CodexHost()
        session = host.get_or_create_session(agent_id="agt_bs")
        claimed = {
            "agent_id": "agt_bs",
            "job": {"job_id": "job_bs"},
            "run": {"run_id": "run_badstatus"},
            "message": {"text": "bad status work"},
        }
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("invalid codex terminal status", str(ctx.exception))

    def test_codex_adapter_recover_sends_interrupt(self) -> None:
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_rec")
        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_rec"})()})()

        adapter.recover(
            host=host,
            session=session,
            claimed={"agent_id": "agt_rec", "job": {"job_id": "j"}, "run": {"run_id": "r"}, "message": {"text": "t"}},
            attempt=1,
            error=RecoverableExecutionError("test"),
            supervisor=SupervisorStub(),
        )
        history = host._history.get(session.session_id, [])
        self.assertTrue(any("INTERRUPT" in entry for entry in history))

    def test_strip_ansi_removes_escape_sequences(self) -> None:
        raw = "\x1b[32mgreen\x1b[0m plain \x1b[1;31mbold-red\x1b[0m"
        self.assertEqual(_strip_ansi(raw), "green plain bold-red")

    def test_strip_ansi_handles_osc_sequences(self) -> None:
        raw = "\x1b]0;title\x07visible"
        self.assertEqual(_strip_ansi(raw), "visible")

    def test_clean_codex_tui_output_strips_chrome(self) -> None:
        raw = (
            "\u256d\u2500\u2500\u2500\u2500\u256e\n"
            "\u2502 Welcome \u2502\n"
            "\u2570\u2500\u2500\u2500\u2500\u256f\n"
            "\u203a What is 2 + 2?\n"
            "\n"
            "\u2022 4\n"
            "\n"
            "gpt-4.1 \u00b7 87% left \u00b7 ~/projects\n"
        )
        cleaned = _clean_codex_tui_output(raw)
        self.assertEqual(cleaned, "4")
        self.assertNotIn("\u256d", cleaned)
        self.assertNotIn("gpt-4.1", cleaned)

    def test_clean_codex_tui_output_extracts_last_turn(self) -> None:
        raw = (
            "\u203a first question\n"
            "\u2022 first answer\n"
            "\u203a second question\n"
            "\u2022 second answer\n"
            "\u2022 with continuation\n"
        )
        cleaned = _clean_codex_tui_output(raw)
        self.assertIn("second answer", cleaned)
        self.assertIn("with continuation", cleaned)
        self.assertNotIn("first answer", cleaned)

    def test_clean_codex_tui_output_strips_noise_lines(self) -> None:
        raw = (
            "\u203a do work\n"
            "\u2022 here is the result\n"
            "Token usage: total=100 input=80 output=20\n"
            "To continue this session, run codex resume abc123\n"
            "Tip: Try the new feature\n"
        )
        cleaned = _clean_codex_tui_output(raw)
        self.assertEqual(cleaned, "here is the result")

    def test_clean_codex_tui_output_preserves_content(self) -> None:
        raw = "line one\nline two\nline three\n"
        cleaned = _clean_codex_tui_output(raw)
        self.assertEqual(cleaned, "line one\nline two\nline three")

    def test_codex_adapter_tui_mode_send_wait_read_cycle(self) -> None:
        class TuiHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("ncodex"):
                    # Simulate Codex TUI ready state with › prompt marker.
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a Summarize recent commits\n"
                    )
                elif text and not text.startswith("ncodex"):
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a explain this code\n\u2022 Here is the result of your task.\n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_tui"})()})()
                self.progress: list[dict] = []

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                self.progress.append({"message": message, "details": details or {}})
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="ncodex",
            idle_poll_seconds=0.0,
            idle_after=1,
            idle_timeout_seconds=0.1,
        )
        host = TuiHost()
        session = host.get_or_create_session(agent_id="agt_tui")
        adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertTrue(session.metadata.get("codex_bootstrapped"))
        history = host._history.get(session.session_id, [])
        self.assertTrue(any("ncodex" in entry for entry in history))

        claimed = {
            "agent_id": "agt_tui",
            "job": {"job_id": "job_tui"},
            "run": {"run_id": "run_tui"},
            "message": {"text": "explain this code"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        roles = [a.role for a in result.artifacts]
        self.assertEqual(roles, ["prompt", "transcript_log", "exec_log", "result"])
        self.assertIn("result of your task", result.artifacts[-1].content)
        self.assertEqual(result.summary["mode"], "tui")

    def test_codex_adapter_tui_mode_empty_output_triggers_recovery(self) -> None:
        class SilentHost(InProcessTerminalHost):
            """Host where sends don't appear in scrollback (simulates TUI input area)."""
            def send_text(self, session, text: str, *, enter: bool = True) -> None:  # noqa: ARG002
                pass

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_empty"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="ncodex",
            idle_poll_seconds=0.0,
            idle_after=1,
            idle_timeout_seconds=0.05,
        )
        host = SilentHost()
        session = host.get_or_create_session(agent_id="agt_empty_tui")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_empty_tui",
            "job": {"job_id": "job_empty_tui"},
            "run": {"run_id": "run_empty_tui"},
            "message": {"text": "silent work"},
        }
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("no output", str(ctx.exception))

    def test_codex_adapter_tui_mode_tmux_launches_prompt_inline_per_run(self) -> None:
        class TmuxTuiHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self.reset_calls = 0
                self.sent: list[str] = []

            def reset_session(self, session):
                self.reset_calls += 1
                return super().reset_session(session)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)
                super().send_text(session, text, enter=enter)
                if "ncodex " in text:
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a What is 2 + 2? Reply with just the number.\n\u2022 4\n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_tmux_tui"})()})()
                self.progress: list[dict] = []

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                self.progress.append({"message": message, "details": details or {}})
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="ncodex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=1,
            idle_timeout_seconds=0.1,
        )
        host = TmuxTuiHost()
        session = host.get_or_create_session(agent_id="agt_tmux_tui")
        adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertTrue(session.metadata.get("codex_bootstrapped"))
        self.assertEqual(host.sent, [])

        claimed = {
            "agent_id": "agt_tmux_tui",
            "job": {"job_id": "job_tmux_tui"},
            "run": {"run_id": "run_tmux_tui"},
            "message": {"text": "What is 2 + 2? Reply with just the number."},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertEqual(host.reset_calls, 1)
        self.assertTrue(any("ncodex --full-auto " in text for text in host.sent))
        self.assertEqual(result.artifacts[-1].content, "4")
        self.assertEqual(result.summary["mode"], "tui")

    def test_codex_adapter_marker_mode_still_works(self) -> None:
        class CodexHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("AGP_RUN_BEGIN run_compat"):
                    self._history.setdefault(session.session_id, []).append(
                        'AGP_RUN_RESULT run_compat {"status":"success","result":"marker result"}\n'
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_compat"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(tui_mode=False, max_polls=2, poll_interval_seconds=0.0)
        host = CodexHost()
        session = host.get_or_create_session(agent_id="agt_compat")
        adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        claimed = {
            "agent_id": "agt_compat",
            "job": {"job_id": "job_compat"},
            "run": {"run_id": "run_compat"},
            "message": {"text": "do compat work"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertEqual(result.artifacts[-1].content, "marker result")

    def test_codex_adapter_tui_bootstrap_times_out_when_cli_never_ready(self) -> None:
        class NeverReadyHost(InProcessTerminalHost):
            """Host where read_visible never shows the Codex ready marker."""
            def read_visible(self, session):
                return "loading...\n"

        adapter = CodexAdapter(tui_mode=True, cli_command="ncodex", idle_poll_seconds=0.0, idle_timeout_seconds=0.01)
        host = NeverReadyHost()
        session = host.get_or_create_session(agent_id="agt_timeout_boot")
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertIn("did not become ready", str(ctx.exception))

    def test_codex_adapter_tui_detects_shell_returned_during_execution(self) -> None:
        call_count = {"n": 0}

        class ExitDuringRunHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("ncodex"):
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a Summarize recent commits\n"
                    )

            def read_visible(self, session):
                call_count["n"] += 1
                if call_count["n"] <= 1:
                    return "\u203a ready\n"
                return "\u276f shell prompt\n"

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_exit"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(tui_mode=True, cli_command="ncodex", idle_poll_seconds=0.0, idle_after=1)
        host = ExitDuringRunHost()
        session = host.get_or_create_session(agent_id="agt_exit_run")
        adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        claimed = {
            "agent_id": "agt_exit_run",
            "job": {"job_id": "job_exit_run"},
            "run": {"run_id": "run_exit_run"},
            "message": {"text": "do work"},
        }
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("exited during execution", str(ctx.exception))

    def test_wezterm_host_cursor_persistence(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        get_text_responses = iter(["baseline\n", "baseline\nnew line\n"])

        def runner(argv: list[str], input: str | None = None, **_: object) -> Result:  # noqa: ARG001
            if argv[2] == "get-text":
                return Result(next(get_text_responses))
            if argv[2] == "list":
                return Result(
                    json.dumps([{"pane_id": 77, "tab_id": 1, "window_id": 1,
                                 "workspace": "agp-test", "window_title": "AGP:agt_persist",
                                 "tab_title": "AGP:agt_persist", "cwd": "/tmp"}])
                )
            raise AssertionError(f"unexpected: {argv}")

        tmp = Path(mkdtemp())
        try:
            host = WezTermHost(workspace="agp-test", runner=runner, checkpoint_dir=tmp)
            session = host.get_or_create_session(agent_id="agt_persist")
            cursor = host.create_cursor(session)
            read = host.read_output(session, cursor)
            self.assertTrue(read.changed)

            cursor_file = tmp / f"cursor-{session.session_id}.json"
            self.assertTrue(cursor_file.exists())

            import json as _json
            saved = _json.loads(cursor_file.read_text())
            self.assertEqual(saved["session_id"], session.session_id)
            self.assertIn("line_count", saved)
            self.assertIn("trailing_hash", saved)
        finally:
            shutil.rmtree(tmp)

    def test_wezterm_host_restart_restores_cursor_and_captures_gap_output(self) -> None:
        """Prove that output produced while the runtime was down is captured after restart."""

        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        phase = {"n": 0}

        def runner(argv: list[str], input: str | None = None, **_: object) -> Result:  # noqa: ARG001
            if argv[2] == "get-text":
                if phase["n"] == 0:
                    return Result("line1\nline2\n")
                if phase["n"] == 1:
                    return Result("line1\nline2\nline3\n")
                return Result("line2\nline3\ngap_output\nnew_output\n")
            if argv[2] == "list":
                return Result(
                    json.dumps([{"pane_id": 44, "tab_id": 1, "window_id": 1,
                                 "workspace": "agp-test", "window_title": "AGP:agt_restart",
                                 "tab_title": "AGP:agt_restart", "cwd": "/tmp"}])
                )
            raise AssertionError(f"unexpected: {argv}")

        tmp = Path(mkdtemp())
        try:
            # Phase 0: first runtime process — create cursor, read output.
            host1 = WezTermHost(workspace="agp-test", runner=runner, checkpoint_dir=tmp)
            session = host1.get_or_create_session(agent_id="agt_restart")
            cursor = host1.create_cursor(session)
            phase["n"] = 1
            read1 = host1.read_output(session, cursor)
            self.assertTrue(read1.changed)
            self.assertIn("line3", read1.text)

            # Phase 2: simulate runtime down, pane produces more output.
            phase["n"] = 2

            # New runtime process — load persisted cursor.
            host2 = WezTermHost(workspace="agp-test", runner=runner, checkpoint_dir=tmp)
            session2 = host2.get_or_create_session(agent_id="agt_restart")
            restored = host2.load_cursor(session2)
            self.assertIsNotNone(restored)
            self.assertTrue(restored.metadata.get("restored"))

            read2 = host2.read_output(session2, restored)
            self.assertTrue(read2.changed)
            self.assertIn("gap_output", read2.text)
            self.assertIn("new_output", read2.text)
        finally:
            shutil.rmtree(tmp)

    def test_tmux_host_restart_restores_absolute_line_cursor(self) -> None:
        """Prove the tmux absolute-line restore path survives restart."""

        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        phase = {"n": 0}

        def runner(argv: list[str], **_: object) -> Result:
            cmd = argv[1]
            if cmd == "has-session":
                return Result(returncode=0)
            if cmd == "new-session":
                return Result()
            if cmd == "display-message":
                if phase["n"] == 0:
                    return Result("5")  # absolute line 5 at cursor creation
                if phase["n"] == 1:
                    return Result("8")  # absolute line 8 after first read
                return Result("12")  # absolute line 12 after restart
            if cmd == "capture-pane":
                if phase["n"] <= 1:
                    return Result("first output\n")
                return Result("gap output during downtime\nnew output\n")
            if cmd == "kill-session":
                return Result()
            return Result()

        from agp.plugins.tmux import TmuxHost
        tmp = Path(mkdtemp())
        try:
            # Phase 0: first runtime — create cursor at absolute line 5.
            host1 = TmuxHost(runner=runner, checkpoint_dir=tmp)
            session = host1.get_or_create_session(agent_id="agt_tmux_restart")
            cursor = host1.create_cursor(session)
            self.assertEqual(cursor.metadata["absolute_line"], 10)  # 5+5 from two display-message calls

            # Phase 1: read output — cursor advances.
            phase["n"] = 1
            read1 = host1.read_output(session, cursor)
            self.assertTrue(read1.changed)

            # Verify cursor file was persisted.
            cursor_file = tmp / f"cursor-{session.session_id}.json"
            self.assertTrue(cursor_file.exists())

            # Phase 2: simulate restart — new host, load cursor.
            phase["n"] = 2
            host2 = TmuxHost(runner=runner, checkpoint_dir=tmp)
            session2 = host2.get_or_create_session(agent_id="agt_tmux_restart")
            restored = host2.load_cursor(session2)
            self.assertIsNotNone(restored)
            self.assertTrue(restored.metadata.get("restored"))

            read2 = host2.read_output(session2, restored)
            self.assertIn("gap output", read2.text)
            self.assertIn("new output", read2.text)
        finally:
            shutil.rmtree(tmp)

    def test_tmux_host_session_lifecycle(self) -> None:
        calls: list[list[str]] = []

        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        state = {"session_exists": False, "captures": 0, "abs_line": 0, "text_sent": False}

        def runner(argv: list[str], **_: object) -> Result:
            calls.append(argv)
            cmd = argv[1]
            if cmd == "has-session":
                return Result(returncode=0 if state["session_exists"] else 1)
            if cmd == "new-session":
                state["session_exists"] = True
                return Result()
            if cmd == "send-keys":
                state["text_sent"] = True
                return Result()
            if cmd == "display-message":
                state["abs_line"] += 1
                return Result(str(state["abs_line"]))
            if cmd == "capture-pane":
                state["captures"] += 1
                if state["text_sent"]:
                    return Result("new output\n")
                return Result("baseline\n")
            if cmd == "kill-session":
                state["session_exists"] = False
                return Result()
            raise AssertionError(f"unexpected tmux command: {argv}")

        from agp.plugins.tmux import TmuxHost
        host = TmuxHost(runner=runner, checkpoint_dir=Path(mkdtemp()))
        session = host.get_or_create_session(agent_id="agt_tmux", workspace_ref="/tmp")
        self.assertEqual(session.session_id, "agp-agt_tmux")
        self.assertTrue(host.session_exists(session))
        health = host.health(session)
        self.assertTrue(health.healthy)

        host.send_text(session, "hello", enter=True)
        send_calls = [c for c in calls if c[1] == "send-keys"]
        self.assertEqual(len(send_calls), 2)  # -l text + Enter as separate calls
        self.assertIn("hello", send_calls[0])
        self.assertIn("Enter", send_calls[1])

        cursor = host.create_cursor(session)
        read = host.read_output(session, cursor)
        self.assertTrue(read.changed)
        self.assertIn("new output", read.text)

        host.interrupt(session)
        interrupt_calls = [c for c in calls if c[1] == "send-keys" and "C-c" in c]
        self.assertEqual(len(interrupt_calls), 1)

        host.terminate_session(session)
        self.assertFalse(host.session_exists(session))

    def test_tmux_host_reuses_existing_session(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def runner(argv: list[str], **_: object) -> Result:
            if argv[1] == "has-session":
                return Result(returncode=0)
            if argv[1] == "display-message":
                return Result("0")
            if argv[1] == "capture-pane":
                return Result("existing\n")
            return Result()

        from agp.plugins.tmux import TmuxHost
        host = TmuxHost(runner=runner, checkpoint_dir=Path(mkdtemp()))
        s1 = host.get_or_create_session(agent_id="agt_reuse")
        s2 = host.get_or_create_session(agent_id="agt_reuse")
        self.assertEqual(s1.session_id, s2.session_id)

    def test_tmux_host_read_visible_captures_current_screen(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        def runner(argv: list[str], **_: object) -> Result:
            if argv[1] == "has-session":
                return Result(returncode=0)
            if argv[1] == "display-message":
                return Result("0")
            if argv[1] == "capture-pane":
                if "-S" in argv:
                    return Result("scrollback content\n")
                return Result("visible screen content\n")
            return Result()

        from agp.plugins.tmux import TmuxHost
        host = TmuxHost(runner=runner, checkpoint_dir=Path(mkdtemp()))
        session = host.get_or_create_session(agent_id="agt_vis")
        visible = host.read_visible(session)
        self.assertEqual(visible, "visible screen content\n")

    def test_tmux_host_works_with_codex_adapter(self) -> None:
        """Prove the plugin boundary: CodexAdapter works with TmuxHost, not just WezTermHost."""

        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        capture_count = {"n": 0}
        marker_line = 'AGP_RUN_RESULT run_tmux_codex {"status":"success","result":"tmux codex result"}\n'

        def runner(argv: list[str], **_: object) -> Result:
            if argv[1] == "has-session":
                return Result(returncode=0)
            if argv[1] == "new-session":
                return Result()
            if argv[1] == "send-keys":
                return Result()
            if argv[1] == "display-message":
                return Result("0")
            if argv[1] == "capture-pane":
                capture_count["n"] += 1
                if "-S" not in argv:
                    return Result("\u203a ready\n")
                # First capture = cursor baseline, subsequent = include marker.
                if capture_count["n"] <= 1:
                    return Result("baseline\n")
                return Result("baseline\n" + marker_line)
            return Result()

        from agp.plugins.tmux import TmuxHost
        host = TmuxHost(runner=runner, checkpoint_dir=Path(mkdtemp()))
        session = host.get_or_create_session(agent_id="agt_tmux_codex")

        adapter = CodexAdapter(tui_mode=False, max_polls=2, poll_interval_seconds=0.0)
        adapter.ensure_bootstrapped(host=host, session=session, claimed={})

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_tmux"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        claimed = {
            "agent_id": "agt_tmux_codex",
            "job": {"job_id": "job_tmux_codex"},
            "run": {"run_id": "run_tmux_codex"},
            "message": {"text": "do tmux work"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertEqual(result.artifacts[-1].content, "tmux codex result")

    def test_plugin_host_cli_round_trip_for_tmux_and_wezterm(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        for host_kind in ("tmux", "wezterm"):
            tmp = Path(mkdtemp())
            try:
                if host_kind == "tmux":
                    state = {"exists": False, "text": "", "abs_line": 0}

                    def runner(argv: list[str], **_: object) -> Result:
                        cmd = argv[1]
                        if cmd == "has-session":
                            return Result(returncode=0 if state["exists"] else 1)
                        if cmd == "new-session":
                            state["exists"] = True
                            return Result()
                        if cmd == "send-keys":
                            if "-l" in argv:
                                state["text"] += argv[-1]
                            elif argv[-1] == "Enter":
                                state["text"] += "\n"
                            elif argv[-1] == "C-c":
                                state["text"] += "INTERRUPT\n"
                            return Result()
                        if cmd == "display-message":
                            state["abs_line"] += 1
                            return Result(str(state["abs_line"]))
                        if cmd == "capture-pane":
                            return Result(state["text"])
                        if cmd == "kill-session":
                            state["exists"] = False
                            return Result()
                        raise AssertionError(f"unexpected tmux argv: {argv}")

                    def factory(kind: str, **kwargs: object):
                        self.assertEqual(kind, "tmux")
                        from agp.plugins.tmux import TmuxHost
                        return TmuxHost(runner=runner, checkpoint_dir=tmp, default_cwd="/tmp")
                else:
                    state = {"exists": False, "text": "", "pane_id": "901"}

                    def runner(argv: list[str], input: str | None = None, **_: object) -> Result:  # noqa: ARG001
                        cmd = argv[2]
                        if cmd == "list":
                            if not state["exists"]:
                                return Result("[]")
                            return Result(json.dumps([{
                                "pane_id": int(state["pane_id"]),
                                "tab_id": 1,
                                "window_id": 1,
                                "workspace": "agp-test",
                                "window_title": "AGP:agt_host",
                                "tab_title": "AGP:agt_host",
                                "cwd": "/tmp",
                            }]))
                        if cmd == "spawn":
                            state["exists"] = True
                            return Result(state["pane_id"])
                        if cmd == "set-window-title" or cmd == "set-tab-title":
                            return Result()
                        if cmd == "send-text":
                            state["text"] += argv[-1]
                            return Result()
                        if cmd == "get-text":
                            return Result(state["text"])
                        if cmd == "kill-pane":
                            state["exists"] = False
                            return Result()
                        raise AssertionError(f"unexpected wezterm argv: {argv}")

                    def factory(kind: str, **kwargs: object):
                        self.assertEqual(kind, "wezterm")
                        return WezTermHost(
                            workspace="agp-test",
                            runner=runner,
                            checkpoint_dir=tmp,
                            default_cwd="/tmp",
                        )

                with patch("agp.plugins.build_terminal_host", side_effect=factory):
                    created = self.cli_runner.invoke(skyops_app, ["host", "create", host_kind, "agt_host", "--workspace-ref", "/tmp"])
                    self.assertEqual(created.exit_code, 0, created.output)
                    created_payload = json.loads(created.stdout)
                    session_id = created_payload["session_id"]

                    sent = self.cli_runner.invoke(skyops_app, ["host", "send", host_kind, session_id, "agt_host", "hello"])
                    self.assertEqual(sent.exit_code, 0, sent.output)

                    read = self.cli_runner.invoke(skyops_app, ["host", "read", host_kind, session_id, "agt_host"])
                    self.assertEqual(read.exit_code, 0, read.output)
                    read_payload = json.loads(read.stdout)
                    self.assertIn("hello", read_payload["full_text"])

                    health = self.cli_runner.invoke(skyops_app, ["host", "health", host_kind, session_id, "agt_host"])
                    self.assertEqual(health.exit_code, 0, health.output)
                    self.assertTrue(json.loads(health.stdout)["healthy"])

                    interrupted = self.cli_runner.invoke(skyops_app, ["host", "interrupt", host_kind, session_id, "agt_host"])
                    self.assertEqual(interrupted.exit_code, 0, interrupted.output)

                    snap = self.cli_runner.invoke(skyops_app, ["host", "snapshot", host_kind, session_id, "agt_host"])
                    self.assertEqual(snap.exit_code, 0, snap.output)

                    terminated = self.cli_runner.invoke(skyops_app, ["host", "terminate", host_kind, session_id, "agt_host"])
                    self.assertEqual(terminated.exit_code, 0, terminated.output)
            finally:
                shutil.rmtree(tmp)

    def test_plugin_adapter_cli_run_once_marker_mode(self) -> None:
        class CodexHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "AGP task instructions:" in text:
                    first_line = text.splitlines()[0]
                    run_id = first_line.split()[-1]
                    marker = f'AGP_RUN_RESULT {run_id} {{"status":"success","result":"marker success"}}\n'
                    self._history.setdefault(session.session_id, []).append(marker)

        tmp = Path(mkdtemp())
        try:
            with (
                patch("agp.plugins.build_terminal_host", return_value=CodexHost()),
                patch("agp.plugins.build_agent_adapter", return_value=CodexAdapter(tui_mode=False, max_polls=2, poll_interval_seconds=0.0)),
            ):
                result = self.cli_runner.invoke(
                    skyops_app,
                    [
                        "adapter", "run-once", "codex", "inprocess", "agt_codex",
                        "--task", "summarize",
                        "--output-root", str(tmp),
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            result_artifact = next(item for item in payload["artifacts"] if item["role"] == "result")
            self.assertEqual(Path(result_artifact["path"]).read_text(encoding="utf-8"), "marker success")
        finally:
            shutil.rmtree(tmp)

    def test_plugin_adapter_cli_run_once_tui_mode(self) -> None:
        class TuiHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.screen = "\u203a ready\n"

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("codex"):
                    self.screen = "\u203a ready\n"
                elif text == "answer the task":
                    self.screen = "\u203a answer the task\n\u2022 tui success\n"

            def read_visible(self, session) -> str:
                return self.screen

            def wait_for_idle(self, session, **kwargs: object) -> bool:  # noqa: ARG002
                return True

        tmp = Path(mkdtemp())
        try:
            with (
                patch("agp.plugins.build_terminal_host", return_value=TuiHost()),
                patch(
                    "agp.plugins.build_agent_adapter",
                    return_value=CodexAdapter(
                        tui_mode=True,
                        cli_command="codex",
                        idle_poll_seconds=0.0,
                        idle_after=1,
                    ),
                ),
            ):
                result = self.cli_runner.invoke(
                    skyops_app,
                    [
                        "adapter", "run-once", "codex", "inprocess", "agt_tui",
                        "--task", "answer the task",
                        "--output-root", str(tmp),
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            result_artifact = next(item for item in payload["artifacts"] if item["role"] == "result")
            self.assertEqual(Path(result_artifact["path"]).read_text(encoding="utf-8"), "tui success")
        finally:
            shutil.rmtree(tmp)

    def test_plugin_run_cli_integrated_inprocess_default(self) -> None:
        tmp = Path(mkdtemp())
        try:
            result = self.cli_runner.invoke(
                skyops_app,
                [
                    "plugin", "run", "inprocess", "default", "agt_plugin",
                    "--task", "hello plugin",
                    "--output-root", str(tmp),
                ],
            )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["host_kind"], "inprocess")
            self.assertEqual(payload["adapter_kind"], "default")
            self.assertTrue(any(item["role"] == "result" for item in payload["artifacts"]))
        finally:
            shutil.rmtree(tmp)

    def test_plugin_run_cli_integrated_tmux_codex(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        capture_count = {"n": 0}
        state = {"run_id": None}

        def runner(argv: list[str], **_: object) -> Result:
            cmd = argv[1]
            if cmd == "has-session":
                return Result(returncode=0)
            if cmd == "new-session":
                return Result()
            if cmd == "send-keys":
                if "-l" in argv and "AGP_RUN_BEGIN" in argv[-1]:
                    state["run_id"] = argv[-1].splitlines()[0].split()[-1]
                return Result()
            if cmd == "display-message":
                return Result("0")
            if cmd == "capture-pane":
                capture_count["n"] += 1
                if capture_count["n"] <= 1:
                    return Result("baseline\n")
                return Result(f'baseline\nAGP_RUN_RESULT {state["run_id"]} {{"status":"success","result":"tmux plugin success"}}\n')
            if cmd == "kill-session":
                return Result()
            raise AssertionError(f"unexpected tmux argv: {argv}")

        tmp = Path(mkdtemp())
        try:
            from agp.plugins.tmux import TmuxHost

            with patch("agp.plugins.build_terminal_host", return_value=TmuxHost(runner=runner, checkpoint_dir=tmp)):
                result = self.cli_runner.invoke(
                    skyops_app,
                    [
                        "plugin", "run", "tmux", "codex", "agt_tmux_plugin",
                        "--task", "tmux task",
                        "--output-root", str(tmp),
                        "--keep-session",
                    ],
                )
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            result_artifact = next(item for item in payload["artifacts"] if item["role"] == "result")
            self.assertEqual(Path(result_artifact["path"]).read_text(encoding="utf-8"), "tmux plugin success")
        finally:
            shutil.rmtree(tmp)
