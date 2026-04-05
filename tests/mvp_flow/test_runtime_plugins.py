"""Runtime host, adapter, plugin, and terminal integration flows."""

import json
import os
import shlex
import subprocess
import sys
from time import time

from tests.mvp_flow.base import *


class MvpFlowRuntimePluginsTest(MvpFlowTestBase):
    def test_launch_command_clears_stale_provider_env_before_exec(self) -> None:
        workspace = self._tmp_root / "workspace-launch-env"
        workspace.mkdir(parents=True, exist_ok=True)

        class LaunchHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_env", workspace_ref=str(workspace))
        probe = (
            "import json, os; "
            "print(json.dumps({"
            "\"OPENAI_API_KEY\": os.environ.get(\"OPENAI_API_KEY\"), "
            "\"ANTHROPIC_API_KEY\": os.environ.get(\"ANTHROPIC_API_KEY\"), "
            "\"OPENAI_BASE_URL\": os.environ.get(\"OPENAI_BASE_URL\")"
            "}))"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}"

        host.launch_command(
            session,
            command=command,
            env={"OPENAI_API_KEY": "fresh-openai-key"},
            cwd=str(workspace),
        )
        script_path = Path(shlex.split(host.sent[-1])[0])
        script_text = script_path.read_text(encoding="utf-8")

        inherited_env = os.environ.copy()
        inherited_env["OPENAI_API_KEY"] = "stale-openai-key"
        inherited_env["ANTHROPIC_API_KEY"] = "stale-anthropic-key"
        inherited_env["OPENAI_BASE_URL"] = "https://stale.example.invalid/v1"
        completed = subprocess.run(
            [str(script_path)],
            check=True,
            capture_output=True,
            text=True,
            env=inherited_env,
        )
        payload = json.loads(completed.stdout)

        self.assertEqual(payload["OPENAI_API_KEY"], "fresh-openai-key")
        self.assertIsNone(payload["ANTHROPIC_API_KEY"])
        self.assertIsNone(payload["OPENAI_BASE_URL"])
        self.assertIn("unset OPENAI_API_KEY", script_text)
        self.assertIn("unset ANTHROPIC_API_KEY", script_text)
        self.assertIn("unset OPENAI_BASE_URL", script_text)
        self.assertIn("export OPENAI_API_KEY=fresh-openai-key", script_text)
        self.assertIn(" -l -c ", script_text)

    def test_launch_command_runs_user_command_via_selected_login_shell_path(self) -> None:
        workspace = self._tmp_root / "workspace-launch-login-shell"
        workspace.mkdir(parents=True, exist_ok=True)
        shell_path = self._tmp_root / "fake-login-shell"
        shell_marker = self._tmp_root / "fake-login-shell-marker"
        shell_path.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "test \"$1\" = \"-l\"\n"
            "test \"$2\" = \"-c\"\n"
            f"printf '%s' \"$0\" > {shlex.quote(str(shell_marker))}\n"
            "exec /bin/sh -lc \"$3\"\n",
            encoding="utf-8",
        )
        shell_path.chmod(0o700)

        class LaunchHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_login_shell", workspace_ref=str(workspace))
        probe = (
            "import json, os; "
            "print(json.dumps({"
            "\"marker\": os.environ.get(\"LOGIN_SHELL_MARKER_FILE\"), "
            "\"openai_api_key\": os.environ.get(\"OPENAI_API_KEY\")"
            "}))"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}"
        with patch.dict(os.environ, {"SHELL": str(shell_path)}):
            host.launch_command(
                session,
                command=command,
                env={
                    "OPENAI_API_KEY": "fresh-openai-key",
                },
                cwd=str(workspace),
            )
            script_path = Path(shlex.split(host.sent[-1])[0])
            script_text = script_path.read_text(encoding="utf-8")

        inherited_env = os.environ.copy()
        inherited_env["SHELL"] = str(shell_path)
        inherited_env["OPENAI_API_KEY"] = "stale-openai-key"
        completed = subprocess.run(
            [str(script_path)],
            check=True,
            capture_output=True,
            text=True,
            env=inherited_env,
        )
        payload = json.loads(completed.stdout)

        self.assertIsNone(payload["marker"])
        self.assertEqual(payload["openai_api_key"], "fresh-openai-key")
        self.assertEqual(shell_marker.read_text(encoding="utf-8"), str(shell_path))
        self.assertIn(str(shell_path), script_text)
        self.assertIn(" -l -c ", script_text)

    def test_pending_launch_script_waits_for_explicit_reap(self) -> None:
        workspace = self._tmp_root / "workspace-launch-pending"
        workspace.mkdir(parents=True, exist_ok=True)

        class LaunchHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)
                super().send_text(session, text, enter=enter)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_pending", workspace_ref=str(workspace))
        host.launch_command(
            session,
            command="printf pending-test",
            env={"OPENAI_API_KEY": "sk-secret"},
            cwd=str(workspace),
        )
        script_path = Path(shlex.split(host.sent[-1])[0])
        script_dir = script_path.parent

        self.assertNotEqual(script_dir, workspace)
        self.assertNotIn(workspace, script_path.parents)
        self.assertIn("sk-secret", script_path.read_text(encoding="utf-8"))
        sleep(0.05)
        self.assertTrue(script_path.exists())
        self.assertTrue(script_dir.exists())

    def test_launch_command_reaps_stale_orphaned_launch_directory_from_prior_process(self) -> None:
        workspace = self._tmp_root / "workspace-launch-orphan-reap"
        workspace.mkdir(parents=True, exist_ok=True)
        launch_root = self._tmp_root / "launch-root"
        stale_dir = launch_root / "agp-launch-stale"
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / ".owner-pid").write_text("99999999\n", encoding="utf-8")
        (stale_dir / ".agp-launch-stale.sh").write_text("secret\n", encoding="utf-8")
        stale_mtime = time() - 10.0
        os.utime(stale_dir, (stale_mtime, stale_mtime))

        class LaunchHost(InProcessTerminalHost):
            _LAUNCH_ROOT_DIR = launch_root
            _STALE_LAUNCH_GRACE_SECONDS = 0.01

            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_orphan_reap", workspace_ref=str(workspace))
        host.launch_command(
            session,
            command="printf orphan-reap-test",
            env={"OPENAI_API_KEY": "sk-secret"},
            cwd=str(workspace),
        )

        self.assertFalse(stale_dir.exists())
        self.assertTrue(Path(shlex.split(host.sent[-1])[0]).exists())

    def test_launch_command_skips_foreign_owned_stale_directory(self) -> None:
        workspace = self._tmp_root / "workspace-launch-foreign-owner"
        workspace.mkdir(parents=True, exist_ok=True)
        launch_root = self._tmp_root / "launch-root-foreign"
        foreign_dir = launch_root / "agp-launch-foreign"
        foreign_dir.mkdir(parents=True, exist_ok=True)
        (foreign_dir / ".owner-pid").write_text("12345\n", encoding="utf-8")
        stale_mtime = time() - 10.0
        os.utime(foreign_dir, (stale_mtime, stale_mtime))

        class LaunchHost(InProcessTerminalHost):
            _LAUNCH_ROOT_DIR = launch_root
            _STALE_LAUNCH_GRACE_SECONDS = 0.01

            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_foreign_owner", workspace_ref=str(workspace))
        with patch("agp.runtime._abc.os.kill", side_effect=PermissionError("foreign process")):
            host.launch_command(
                session,
                command="printf foreign-owner-test",
                env={"OPENAI_API_KEY": "sk-secret"},
                cwd=str(workspace),
            )

        self.assertTrue(foreign_dir.exists())
        self.assertTrue(Path(shlex.split(host.sent[-1])[0]).exists())

    def test_reset_session_reaps_pending_launch_scripts(self) -> None:
        workspace = self._tmp_root / "workspace-launch-reset-cleanup"
        workspace.mkdir(parents=True, exist_ok=True)

        class LaunchHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_reset", workspace_ref=str(workspace))
        host.launch_command(
            session,
            command="printf reset-cleanup-test",
            env={"OPENAI_API_KEY": "sk-secret"},
            cwd=str(workspace),
        )
        script_path = Path(shlex.split(host.sent[-1])[0])
        script_dir = script_path.parent

        reset = host.reset_session(session)

        self.assertFalse(script_path.exists())
        self.assertFalse(script_dir.exists())
        self.assertNotIn("_pending_launch_scripts", reset.metadata)

    def test_second_launch_reaps_prior_pending_launch_script(self) -> None:
        workspace = self._tmp_root / "workspace-launch-repeat-cleanup"
        workspace.mkdir(parents=True, exist_ok=True)

        class LaunchHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_repeat", workspace_ref=str(workspace))
        host.launch_command(
            session,
            command="printf first-launch",
            env={"OPENAI_API_KEY": "sk-first"},
            cwd=str(workspace),
        )
        first_script_path = Path(shlex.split(host.sent[-1])[0])
        first_script_dir = first_script_path.parent

        host.launch_command(
            session,
            command="printf second-launch",
            env={"OPENAI_API_KEY": "sk-second"},
            cwd=str(workspace),
        )
        second_script_path = Path(shlex.split(host.sent[-1])[0])

        self.assertFalse(first_script_path.exists())
        self.assertFalse(first_script_dir.exists())
        self.assertTrue(second_script_path.exists())
        pending = session.metadata.get("_pending_launch_scripts", [])
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["script_path"], str(second_script_path))

    def test_terminate_session_reaps_pending_launch_scripts(self) -> None:
        workspace = self._tmp_root / "workspace-launch-terminate-cleanup"
        workspace.mkdir(parents=True, exist_ok=True)

        class LaunchHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)

        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch_terminate", workspace_ref=str(workspace))
        host.launch_command(
            session,
            command="printf terminate-cleanup-test",
            env={"OPENAI_API_KEY": "sk-secret"},
            cwd=str(workspace),
        )
        script_path = Path(shlex.split(host.sent[-1])[0])
        script_dir = script_path.parent

        host.terminate_session(session)

        self.assertFalse(script_path.exists())
        self.assertFalse(script_dir.exists())

    def test_codex_adapter_appends_output_contract_instruction(self) -> None:
        class ContractHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)
                super().send_text(session, text, enter=enter)
                if text.startswith("AGP_RUN_BEGIN run_contract"):
                    self._history.setdefault(session.session_id, []).append(
                        'AGP_RUN_RESULT run_contract {"status":"success","result":"{\\"status\\":\\"ok\\"}"}\n'
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_codex_contract"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)
        host = ContractHost()
        session = host.get_or_create_session(agent_id="agt_codex_contract")
        claimed = {
            "agent_id": "agt_codex_contract",
            "job": {
                "job_id": "job_codex_contract",
                "output_contract_json": {
                    "format": "json",
                    "json_schema": {
                        "type": "object",
                        "required": ["status"],
                        "properties": {"status": {"type": "string"}},
                    },
                },
            },
            "run": {"run_id": "run_contract"},
            "message": {"text": "return structured output"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        sent = host.sent[-1]
        # Via-file: the marker envelope contains a reference string, not the full prompt
        self.assertIn("Read the file", sent)
        self.assertIn("agp-task-run_contract.md", sent)
        # The marker envelope still wraps it with AGP_RUN_BEGIN
        self.assertIn("AGP_RUN_BEGIN run_contract", sent)
        self.assertEqual(result.artifacts[-1].content, '{"status":"ok"}')

    def test_claude_code_adapter_appends_output_contract_instruction(self) -> None:
        class ContractHost(InProcessTerminalHost):
            def __init__(self, screens: list[str]) -> None:
                super().__init__()
                self._screens = list(screens)
                self._last_screen = screens[-1] if screens else ""
                self.sent: list[str] = []

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)
                super().send_text(session, text, enter=enter)

            def read_visible(self, session) -> str:  # noqa: ARG002
                if self._screens:
                    self._last_screen = self._screens.pop(0)
                return self._last_screen

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_contract"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.0,
            idle_after=1,
            idle_timeout_seconds=0.2,
        )
        screen_content = (
            "\u276f return structured output\n"
            "\u23fa {\"status\":\"ok\"}\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\n"
        )
        host = ContractHost(["", screen_content, screen_content])
        session = host.get_or_create_session(agent_id="agt_cc_contract")
        claimed = {
            "agent_id": "agt_cc_contract",
            "job": {
                "job_id": "job_cc_contract",
                "output_contract_json": {
                    "format": "json",
                    "json_schema": {
                        "type": "object",
                        "required": ["status"],
                        "properties": {"status": {"type": "string"}},
                    },
                },
            },
            "run": {"run_id": "run_cc_contract"},
            "message": {"text": "return structured output"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        sent = host.sent[-1]
        # Via-file: the TUI receives a reference string, not the full prompt
        self.assertIn("Read the file", sent)
        self.assertIn("agp-task-run_cc_contract.md", sent)
        # The task file artifact should contain the output contract
        task_file_artifact = [a for a in result.artifacts if a.name == "task-file.md"]
        self.assertEqual(len(task_file_artifact), 1)
        self.assertIn("Output Contract", task_file_artifact[0].content)
        self.assertIn('"required"', task_file_artifact[0].content)
        self.assertEqual(result.artifacts[-1].content, '{"status":"ok"}')

    def test_claude_code_contract_falls_back_to_terminal_json_when_result_file_is_invalid(self) -> None:
        from pathlib import Path

        from agp.plugins._output_contracts import result_file_path_for_run
        from agp.runtime._types import OutputCursor, OutputReadResult

        class InvalidFileHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self, *, run_id: str) -> None:
                super().__init__()
                self._visible_reads = 0
                self._result_path = Path(result_file_path_for_run(run_id))

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                self._result_path.write_text('{"verdict":"changes_requested"', encoding="utf-8")

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u276f old prompt\n\u23fa OLD_OK\n"
                return (
                    "\u276f Review output\n"
                    "\u25cf I found two issues.\n"
                    '{"verdict":"changes_requested","summary":"needs fixes"}\n'
                    "\n"
                    "\u276f \n"
                )

            def read_output(self, session, cursor):
                full_text = (
                    "\u276f Review output\n"
                    "\u25cf I found two issues.\n"
                    '{"verdict":"changes_requested","summary":"needs fixes"}\n'
                    "\n"
                )
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": 1}),
                    text=full_text,
                    full_text=full_text,
                    changed=True,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_cc_json_fallback"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        run_id = "run_cc_invalid_file"
        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = InvalidFileHost(run_id=run_id)
        session = host.get_or_create_session(agent_id="agt_cc_invalid_file")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_cc_invalid_file",
            "job": {
                "job_id": "job_cc_invalid_file",
                "output_contract_json": {
                    "format": "json",
                    "json_schema": {
                        "type": "object",
                        "required": ["verdict", "summary"],
                    },
                },
            },
            "run": {"run_id": run_id},
            "message": {"text": "Review output"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_log = next(artifact for artifact in result.artifacts if artifact.name == "result.txt")
        self.assertEqual(result_log.content, '{"verdict":"changes_requested","summary":"needs fixes"}')

    def test_codex_timeout_failure_result_salvages_tmux_pane_artifact(self) -> None:
        from agp.runtime import ExecutionTimeout

        class SnapshotHost(InProcessTerminalHost):
            kind = "tmux"

            def snapshot(self, session):
                data = super().snapshot(session)
                data["text"] = "partial model output\nstill useful\n"
                return data

            def read_visible(self, session) -> str:
                return "visible tail only\n"

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_tmux_timeout"})()})()

        adapter = CodexAdapter()
        host = SnapshotHost()
        session = host.get_or_create_session(agent_id="agt_tmux_timeout")
        result = adapter.build_failure_result(
            host=host,
            session=session,
            claimed={
                "agent_id": "agt_tmux_timeout",
                "job": {
                    "job_id": "job_tmux_timeout",
                    "output_contract_json": {
                        "format": "json",
                        "json_schema": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                        },
                    },
                },
                "run": {"run_id": "run_tmux_timeout"},
                "message": {"text": "unfinished work"},
            },
            error=ExecutionTimeout("codex timed out"),
            supervisor=SupervisorStub(),
        )
        pane_artifact = next(a for a in result.artifacts if a.name == "tmux-pane.txt")
        self.assertEqual(pane_artifact.role, "failure_evidence")
        self.assertIn("partial model output", pane_artifact.content)
        prompt_artifact = next(a for a in result.artifacts if a.name == "prompt.txt")
        self.assertIn("IMPORTANT: You must respond with valid JSON matching this schema:", prompt_artifact.content)

    def test_claude_code_failure_result_uses_augmented_prompt_for_output_contract(self) -> None:
        from agp.runtime import AdapterExecutionFailed

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_cc_failure"})()})()

        adapter = ClaudeCodeAdapter()
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_cc_failure")
        result = adapter.build_failure_result(
            host=host,
            session=session,
            claimed={
                "agent_id": "agt_cc_failure",
                "job": {
                    "job_id": "job_cc_failure",
                    "output_contract_json": {
                        "format": "json",
                        "json_schema": {
                            "type": "object",
                            "properties": {"status": {"type": "string"}},
                        },
                    },
                },
                "run": {"run_id": "run_cc_failure"},
                "message": {"text": "return structured output"},
            },
            error=AdapterExecutionFailed("adapter failed", transcript="transcript", output="exec log"),
            supervisor=SupervisorStub(),
        )
        prompt_artifact = next(a for a in result.artifacts if a.name == "prompt.txt")
        self.assertIn("IMPORTANT: You must respond with valid JSON matching this schema:", prompt_artifact.content)

    def test_claude_code_indeterminate_failure_result_matches_fail_contract(self) -> None:
        from agp.runtime import StableButIndeterminate

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_cc_indeterminate_fail"})()})()

        adapter = ClaudeCodeAdapter()
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_cc_indeterminate_fail")
        result = adapter.build_failure_result(
            host=host,
            session=session,
            claimed={
                "agent_id": "agt_cc_indeterminate_fail",
                "job": {"job_id": "job_cc_indeterminate_fail"},
                "run": {"run_id": "run_cc_indeterminate_fail"},
                "message": {"text": "review this"},
            },
            error=StableButIndeterminate(
                "screen is stable but adapter cannot determine if the agent completed, is waiting for input, or is stuck",
                screen="final visible screen",
                last_good_screen="cleaned response",
            ),
            supervisor=SupervisorStub(),
        )

        roles = {artifact.name: artifact.role for artifact in result.artifacts}
        self.assertEqual(roles["prompt.txt"], "prompt")
        self.assertEqual(roles["transcript.txt"], "transcript_log")
        self.assertEqual(roles["exec.txt"], "exec_log")
        self.assertEqual(roles["screen.txt"], "failure_evidence")
        self.assertEqual(roles["failure.txt"], "failure_evidence")

    def test_codex_indeterminate_failure_result_matches_fail_contract(self) -> None:
        from agp.runtime import StableButIndeterminate

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_codex_indeterminate_fail"})()})()

        adapter = CodexAdapter()
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_codex_indeterminate_fail")
        result = adapter.build_failure_result(
            host=host,
            session=session,
            claimed={
                "agent_id": "agt_codex_indeterminate_fail",
                "job": {"job_id": "job_codex_indeterminate_fail"},
                "run": {"run_id": "run_codex_indeterminate_fail"},
                "message": {"text": "review this"},
            },
            error=StableButIndeterminate(
                "screen is stable but adapter cannot determine if the agent completed, is waiting for input, or is stuck",
                screen="final visible screen",
                last_good_screen="cleaned response",
            ),
            supervisor=SupervisorStub(),
        )

        roles = {artifact.name: artifact.role for artifact in result.artifacts}
        self.assertEqual(roles["prompt.txt"], "prompt")
        self.assertEqual(roles["transcript.txt"], "transcript_log")
        self.assertEqual(roles["exec.txt"], "exec_log")
        self.assertEqual(roles["screen.txt"], "failure_evidence")
        self.assertEqual(roles["failure.txt"], "failure_evidence")

    def test_codex_adapter_marker_mode_emits_idle_heartbeat_before_timeout(self) -> None:
        from agp.runtime._types import OutputReadResult

        class QuietHost(InProcessTerminalHost):
            def read_output(self, session, cursor):
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=cursor,
                    text="",
                    full_text="",
                    changed=False,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_idle_hb"})()})()
                self.progress: list[dict] = []

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                self.progress.append({"message": message, "details": details or {}})
                return {"status": "ok"}

        adapter = CodexAdapter(
            poll_interval_seconds=0.01,
            idle_timeout_seconds=0.02,
        )
        host = QuietHost()
        session = host.get_or_create_session(agent_id="agt_idle_hb")
        supervisor = SupervisorStub()
        claimed = {
            "agent_id": "agt_idle_hb",
            "job": {"job_id": "job_idle_hb"},
            "run": {"run_id": "run_idle_hb"},
            "message": {"text": "wait for marker"},
        }
        with self.assertRaises(RecoverableExecutionError):
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=supervisor)
        self.assertTrue(any(item["details"].get("changed") is False for item in supervisor.progress))

    def test_codex_adapter_marker_mode_extends_idle_timeout_while_output_changes(self) -> None:
        from agp.runtime._types import OutputCursor, OutputReadResult

        class StreamingHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self._reads = 0

            def read_output(self, session, cursor):
                self._reads += 1
                updates = {
                    1: "thinking\n",
                    2: "still thinking\n",
                    3: 'AGP_RUN_RESULT run_stream {"status":"success","result":"done"}\n',
                }
                text = updates.get(self._reads, "")
                full_text = "".join(updates[idx] for idx in range(1, self._reads + 1) if idx in updates)
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": self._reads}),
                    text=text,
                    full_text=full_text,
                    changed=bool(text),
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_stream"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            poll_interval_seconds=0.02,
            idle_timeout_seconds=0.2,
        )
        host = StreamingHost()
        session = host.get_or_create_session(agent_id="agt_stream")
        claimed = {
            "agent_id": "agt_stream",
            "job": {"job_id": "job_stream"},
            "run": {"run_id": "run_stream"},
            "message": {"text": "stream until done"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertEqual(result.artifacts[-1].content, "done")

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
            def launch_command(self, session, *, command, env=None, cwd=None):
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("codex"):
                    # Simulate Codex TUI ready state with › prompt marker.
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a Summarize recent commits\n"
                    )
                elif text and not text.startswith("codex"):
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a explain this code\n\u2022 Here is the result of your task.\n\n\u203a \n"
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
            cli_command="codex",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
        )
        host = TuiHost()
        session = host.get_or_create_session(agent_id="agt_tui")
        adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertTrue(session.metadata.get("codex_bootstrapped"))
        history = host._history.get(session.session_id, [])
        self.assertTrue(any("codex" in entry for entry in history))

        claimed = {
            "agent_id": "agt_tui",
            "job": {"job_id": "job_tui"},
            "run": {"run_id": "run_tui"},
            "message": {"text": "explain this code"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        roles = [a.role for a in result.artifacts]
        self.assertEqual(roles, ["prompt", "prompt", "transcript_log", "exec_log", "result"])
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
            cli_command="codex",
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

    def test_codex_tui_bootstrap_launches_hidden_command_with_provider_env(self) -> None:
        class LaunchHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "wezterm"

            def __init__(self) -> None:
                super().__init__()
                self.launches: list[tuple[str, dict[str, str] | None, str | None]] = []
                self.sent: list[str] = []

            def launch_command(self, session, *, command: str, env: dict[str, str] | None = None, cwd: str | None = None):
                self.launches.append((command, env, cwd))
                self._history.setdefault(session.session_id, []).append("›\n")
                return None

            def read_visible(self, session) -> str:
                return "".join(self._history.get(session.session_id, []))

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)
                super().send_text(session, text, enter=enter)

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_timeout_seconds=0.1,
        )
        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_launch")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
            adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertEqual(len(host.launches), 1)
        command, env, cwd = host.launches[0]
        self.assertEqual(command, "codex --full-auto")
        self.assertEqual(env["OPENAI_API_KEY"], "sk-test")
        self.assertIsNone(cwd)
        self.assertEqual(host.sent, [])

    def test_claude_code_bootstrap_launches_hidden_command_with_provider_env(self) -> None:
        class LaunchHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "wezterm"

            def __init__(self) -> None:
                super().__init__()
                self.launches: list[tuple[str, dict[str, str] | None, str | None]] = []

            def launch_command(self, session, *, command: str, env: dict[str, str] | None = None, cwd: str | None = None):
                self.launches.append((command, env, cwd))
                self._history.setdefault(session.session_id, []).append("Claude Code\n────────\n❯\n")
                return None

            def read_visible(self, session) -> str:
                return "".join(self._history.get(session.session_id, []))

        adapter = ClaudeCodeAdapter(
            cli_command="claude",
            idle_poll_seconds=0.0,
            idle_timeout_seconds=0.1,
        )
        host = LaunchHost()
        session = host.get_or_create_session(agent_id="agt_claude_launch")
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "tok-test"}, clear=False):
            adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertEqual(len(host.launches), 1)
        command, env, cwd = host.launches[0]
        self.assertEqual(command, "claude --dangerously-skip-permissions")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "tok-test")
        self.assertIsNone(cwd)

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

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                self.sent.append(text)
                super().send_text(session, text, enter=enter)
                if "codex " in text:
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a What is 2 + 2? Reply with just the number.\n\u2022 4\n\n\u203a \n"
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
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
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
        self.assertTrue(any("codex --full-auto " in text for text in host.sent))
        self.assertEqual(result.artifacts[-1].content, "4")
        self.assertEqual(result.summary["mode"], "tui")

    def test_codex_adapter_tui_completes_when_response_returns_to_prompt(self) -> None:
        class PromptReturnHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "codex " in text:
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a Delegate to agt_local\n"
                        "\u2022 LOCAL_OK\n"
                        "\n"
                        "\u203a Write tests for @filename\n"
                        "  gpt-5.3-codex default · 100% left · /app\n"
                    )

            def read_visible(self, session):
                base = super().read_visible(session)
                # Simulate a repainting Codex status bar after the response.
                return base + "\n  gpt-5.3-codex default · 100% left · /app\n"

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_prompt_return"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = PromptReturnHost()
        session = host.get_or_create_session(agent_id="agt_prompt_return")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_prompt_return",
            "job": {"job_id": "job_prompt_return"},
            "run": {"run_id": "run_prompt_return"},
            "message": {"text": "delegate and summarize"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertEqual(result.artifacts[-1].content, "LOCAL_OK")

    def test_codex_adapter_tui_tmux_oneshot_shell_return_after_new_turn_succeeds(self) -> None:
        class OneShotCompleteHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "codex " in text:
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a previous prompt\n"
                        "\u2022 PREVIOUS_OK\n"
                        "\n"
                        "\u203a tmux oneshot task\n"
                        "\u2022 NEW_OK\n"
                        "\n"
                    )

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u203a previous prompt\n\u2022 PREVIOUS_OK\n"
                return (
                    "\u203a previous prompt\n"
                    "\u2022 PREVIOUS_OK\n"
                    "\n"
                    "\u203a tmux oneshot task\n"
                    "\u2022 NEW_OK\n"
                    "\n"
                    "shell resumed\n"
                    "pwd\n"
                    "/workspace\n"
                    "echo done\n"
                    "done\n"
                    "$ \n"
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_tmux_oneshot_ok"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = OneShotCompleteHost()
        session = host.get_or_create_session(agent_id="agt_tmux_oneshot_ok")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_tmux_oneshot_ok",
            "job": {"job_id": "job_tmux_oneshot_ok"},
            "run": {"run_id": "run_tmux_oneshot_ok"},
            "message": {"text": "tmux oneshot task"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        exec_log = next(artifact for artifact in result.artifacts if artifact.name == "exec.txt")
        self.assertIn("NEW_OK", exec_log.content)

    def test_codex_adapter_tui_tmux_oneshot_shell_return_after_new_turn_scrolled_out_succeeds(self) -> None:
        from agp.runtime._types import OutputCursor, OutputReadResult

        class OneShotScrolledCompleteHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0
                self._output_reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u203a previous prompt\n\u2022 PREVIOUS_OK\n"
                return "shell resumed\npwd\n/workspace\n$ \n"

            def read_output(self, session, cursor):
                self._output_reads += 1
                full_text = (
                    "\u203a previous prompt\n"
                    "\u2022 PREVIOUS_OK\n"
                    "\n"
                    "\u203a tmux oneshot task\n"
                    "\u2022 NEW_OK\n"
                    "\n"
                    "shell resumed\n"
                    "pwd\n"
                    "/workspace\n"
                    "$ \n"
                )
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": self._output_reads}),
                    text=full_text if self._output_reads == 1 else "",
                    full_text=full_text,
                    changed=self._output_reads == 1,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_tmux_oneshot_scrolled_ok"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = OneShotScrolledCompleteHost()
        session = host.get_or_create_session(agent_id="agt_tmux_oneshot_scrolled_ok")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_tmux_oneshot_scrolled_ok",
            "job": {"job_id": "job_tmux_oneshot_scrolled_ok"},
            "run": {"run_id": "run_tmux_oneshot_scrolled_ok"},
            "message": {"text": "tmux oneshot task"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        transcript_log = next(artifact for artifact in result.artifacts if artifact.name == "transcript.txt")
        exec_log = next(artifact for artifact in result.artifacts if artifact.name == "exec.txt")
        result_log = next(artifact for artifact in result.artifacts if artifact.name == "result.txt")
        self.assertIn("NEW_OK", transcript_log.content)
        self.assertIn("NEW_OK", exec_log.content)
        self.assertIn("NEW_OK", result_log.content)

    def test_codex_adapter_tui_prefers_visible_screen_over_stale_exec_log_for_result(self) -> None:
        from agp.runtime._types import OutputCursor, OutputReadResult

        class VisibleWinsHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0
                self._output_reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u203a old prompt\n\u2022 OLD_OK\n"
                return (
                    "\u203a Reply with exactly: pong\n"
                    "\u2022 pong\n"
                    "\n"
                    "\u203a Explain this codebase\n"
                    "  gpt-5.4 medium \u00b7 100% left \u00b7 ~/projects/skynet\n"
                )

            def read_output(self, session, cursor):
                self._output_reads += 1
                full_text = "Working (1s \u2022 esc to interrupt)\n"
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": self._output_reads}),
                    text=full_text if self._output_reads == 1 else "",
                    full_text=full_text,
                    changed=self._output_reads == 1,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_visible_wins"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = VisibleWinsHost()
        session = host.get_or_create_session(agent_id="agt_visible_wins")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_visible_wins",
            "job": {"job_id": "job_visible_wins"},
            "run": {"run_id": "run_visible_wins"},
            "message": {"text": "Reply with exactly: pong"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        transcript_log = next(artifact for artifact in result.artifacts if artifact.name == "transcript.txt")
        exec_log = next(artifact for artifact in result.artifacts if artifact.name == "exec.txt")
        result_log = next(artifact for artifact in result.artifacts if artifact.name == "result.txt")
        self.assertIn("pong", transcript_log.content)
        self.assertEqual(exec_log.content, "Working (1s \u2022 esc to interrupt)\n")
        self.assertEqual(result_log.content, "pong")

    def test_extract_codex_tui_result_prefers_newer_accumulated_turn_over_stale_visible_turn(self) -> None:
        visible_output = (
            "\u203a old prompt\n"
            "\u2022 OLD_OK\n"
            "\n"
            "\u203a Explain this codebase\n"
            "  gpt-5.4 medium \u00b7 100% left \u00b7 ~/projects/skynet\n"
        )
        raw_output = (
            "\u203a old prompt\n"
            "\u2022 OLD_OK\n"
            "\n"
            "\u203a Reply with exactly: pong\n"
            "\u2022 pong\n"
            "\n"
        )
        self.assertEqual(
            _extract_codex_tui_result(visible_output, raw_output, baseline_last_response="OLD_OK"),
            "pong",
        )

    def test_codex_adapter_tui_extracts_trailing_json_for_output_contract_jobs(self) -> None:
        from agp.runtime._types import OutputCursor, OutputReadResult

        class JsonContractHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u203a old prompt\n\u2022 OLD_OK\n"
                return (
                    "\u203a Review output\n"
                    "\u2022 I found two issues.\n"
                    '{"verdict":"changes_requested","summary":"needs fixes"}\n'
                    "\n"
                    "\u203a Next prompt\n"
                )

            def read_output(self, session, cursor):
                full_text = (
                    "\u203a Review output\n"
                    "\u2022 I found two issues.\n"
                    '{"verdict":"changes_requested","summary":"needs fixes"}\n'
                    "\n"
                )
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": 1}),
                    text=full_text,
                    full_text=full_text,
                    changed=True,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_json_contract"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = JsonContractHost()
        session = host.get_or_create_session(agent_id="agt_json_contract")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_json_contract",
            "job": {
                "job_id": "job_json_contract",
                "output_contract_json": {
                    "format": "json",
                    "json_schema": {
                        "type": "object",
                        "required": ["verdict", "summary"],
                    },
                },
            },
            "run": {"run_id": "run_json_contract"},
            "message": {"text": "Review output"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_log = next(artifact for artifact in result.artifacts if artifact.name == "result.txt")
        self.assertEqual(result_log.content, '{"verdict":"changes_requested","summary":"needs fixes"}')

    def test_codex_adapter_tui_recovers_wrapped_json_for_output_contract_jobs(self) -> None:
        from agp.runtime._types import OutputCursor, OutputReadResult

        class WrappedJsonHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u203a old prompt\n\u2022 OLD_OK\n"
                return (
                    "\u203a Review output\n"
                    "\u2022 notes first\n"
                    '{"verdict":"changes_requested","summary":"wrapped re\n'
                    'view"}\n'
                    "\n"
                    "\u203a Next prompt\n"
                )

            def read_output(self, session, cursor):
                full_text = (
                    "\u203a Review output\n"
                    "\u2022 notes first\n"
                    '{"verdict":"changes_requested","summary":"wrapped re\n'
                    'view"}\n'
                    "\n"
                )
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": 1}),
                    text=full_text,
                    full_text=full_text,
                    changed=True,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_wrapped_json_contract"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = WrappedJsonHost()
        session = host.get_or_create_session(agent_id="agt_wrapped_json_contract")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_wrapped_json_contract",
            "job": {
                "job_id": "job_wrapped_json_contract",
                "output_contract_json": {
                    "format": "json",
                    "json_schema": {
                        "type": "object",
                        "required": ["verdict", "summary"],
                    },
                },
            },
            "run": {"run_id": "run_wrapped_json_contract"},
            "message": {"text": "Review output"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_log = next(artifact for artifact in result.artifacts if artifact.name == "result.txt")
        self.assertEqual(result_log.content, '{"verdict":"changes_requested","summary":"wrapped review"}')

    def test_codex_adapter_tui_prefers_visible_turn_when_turn_counts_tie(self) -> None:
        from agp.runtime._types import OutputCursor, OutputReadResult

        class VisibleTieWinsHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0
                self._output_reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u203a old prompt\n\u2022 OLD_OK\n"
                return (
                    "\u203a Reply with exactly: ok\n"
                    "\u2022 ok\n"
                    "\n"
                    "\u203a Explain this codebase\n"
                    "  gpt-5.4 medium \u00b7 100% left \u00b7 ~/projects/skynet\n"
                )

            def read_output(self, session, cursor):
                self._output_reads += 1
                full_text = (
                    "\u203a Reply with exactly: very long historical answer\n"
                    "\u2022 very long historical answer\n"
                    "\n"
                )
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": self._output_reads}),
                    text=full_text if self._output_reads == 1 else "",
                    full_text=full_text,
                    changed=self._output_reads == 1,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_visible_tie_wins"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = VisibleTieWinsHost()
        session = host.get_or_create_session(agent_id="agt_visible_tie_wins")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_visible_tie_wins",
            "job": {"job_id": "job_visible_tie_wins"},
            "run": {"run_id": "run_visible_tie_wins"},
            "message": {"text": "Reply with exactly: ok"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        transcript_log = next(artifact for artifact in result.artifacts if artifact.name == "transcript.txt")
        result_log = next(artifact for artifact in result.artifacts if artifact.name == "result.txt")
        self.assertIn("• ok", transcript_log.content)
        self.assertEqual(result_log.content, "ok")

    def test_repair_json_string_fixes_unescaped_interior_quotes(self) -> None:
        from agp.plugins.codex import _repair_json_string
        # Simple unescaped quote
        self.assertEqual(
            json.loads(_repair_json_string('{"a":"he said "hello" today"}')),
            {"a": 'he said "hello" today'},
        )
        # Unescaped quote followed by colon (the case the reviewer flagged)
        self.assertEqual(
            json.loads(_repair_json_string('{"summary":"he said "x": y"}')),
            {"summary": 'he said "x": y'},
        )
        # Multiple unescaped quotes in different fields
        self.assertEqual(
            json.loads(_repair_json_string(
                '{"a":"uses "foo" lib","b":"calls "bar" api"}'
            )),
            {"a": 'uses "foo" lib', "b": 'calls "bar" api'},
        )
        # Already-valid JSON is returned unchanged
        valid = '{"ok":true,"msg":"clean"}'
        self.assertEqual(_repair_json_string(valid), valid)
        # Backslash-parity: even backslashes before quote means unescaped
        self.assertEqual(
            json.loads(_repair_json_string('{"a":"X\\\\"Y"}')),
            {"a": 'X\\"Y'},
        )

    def test_extract_trailing_json_text_repairs_unescaped_quotes(self) -> None:
        from agp.plugins.codex import _extract_trailing_json_text
        text = (
            "Here is my review.\n"
            '{"verdict":"changes_requested","summary":"fails on "nested escapes" badly"}'
        )
        result = _extract_trailing_json_text(text)
        self.assertIsNotNone(result)
        parsed = json.loads(result)
        self.assertEqual(parsed["verdict"], "changes_requested")
        self.assertIn("nested escapes", parsed["summary"])

    def test_codex_adapter_tui_tmux_oneshot_shell_return_without_new_turn_raises_pane_died(self) -> None:
        class OneShotCrashHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u203a previous prompt\n\u2022 PREVIOUS_OK\n"
                return "$ \n"

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_tmux_oneshot_crash"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = OneShotCrashHost()
        session = host.get_or_create_session(agent_id="agt_tmux_oneshot_crash")
        session.metadata["codex_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_tmux_oneshot_crash",
            "job": {"job_id": "job_tmux_oneshot_crash"},
            "run": {"run_id": "run_tmux_oneshot_crash"},
            "message": {"text": "tmux oneshot task"},
        }

        with self.assertRaises(PaneDied) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("codex cli exited during execution", str(ctx.exception))

    def test_codex_adapter_tui_ignores_stale_completed_turns_from_prior_prompt(self) -> None:
        class StalePromptHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "wezterm"

            def __init__(self) -> None:
                super().__init__()
                self.reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                self._history.setdefault(session.session_id, []).append(
                    "\u203a old prompt\n"
                    "\u2022 OLD_OK\n"
                    "\n"
                    "\u203a delegate and summarize\n"
                    "\u2022 LOCAL_OK\n"
                    "\n"
                    "\u203a\n"
                )

            def read_visible(self, session):
                self.reads += 1
                return super().read_visible(session)

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_stale_prompt"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = StalePromptHost()
        session = host.get_or_create_session(agent_id="agt_stale_prompt")
        session.metadata["codex_bootstrapped"] = True
        host._history.setdefault(session.session_id, []).append(
            "\u203a old prompt\n"
            "\u2022 OLD_OK\n"
            "\n"
            "\u203a\n"
        )
        claimed = {
            "agent_id": "agt_stale_prompt",
            "job": {"job_id": "job_stale_prompt"},
            "run": {"run_id": "run_stale_prompt"},
            "message": {"text": "delegate and summarize"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertEqual(result.artifacts[-1].content, "LOCAL_OK")

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

        adapter = CodexAdapter(tui_mode=True, cli_command="codex", idle_poll_seconds=0.0, idle_timeout_seconds=0.01)
        host = NeverReadyHost()
        session = host.get_or_create_session(agent_id="agt_timeout_boot")
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertIn("did not become ready", str(ctx.exception))

    def test_codex_adapter_detects_onboarding_prompt(self) -> None:
        adapter = CodexAdapter(tui_mode=True)
        screen = (
            "Welcome to Codex, OpenAI's command-line coding agent\n"
            "1. Sign in with ChatGPT\n"
            "2. Sign in with Device Code\n"
            "3. Provide your own API key\n"
            "Press Enter to continue\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(screen))

    def test_codex_adapter_prefers_api_key_on_onboarding_when_key_present(self) -> None:
        adapter = CodexAdapter(tui_mode=True)
        screen = (
            "Welcome to Codex, OpenAI's command-line coding agent\n"
            "1. Sign in with ChatGPT\n"
            "2. Sign in with Device Code\n"
            "3. Provide your own API key\n"
            "Press Enter to continue\n"
        )
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}, clear=False):
            self.assertEqual(adapter._gate_response(screen), "3")

    def test_codex_adapter_prefers_chatgpt_login_on_onboarding_without_key(self) -> None:
        adapter = CodexAdapter(tui_mode=True)
        screen = (
            "Welcome to Codex, OpenAI's command-line coding agent\n"
            "1. Sign in with ChatGPT\n"
            "2. Sign in with Device Code\n"
            "3. Provide your own API key\n"
            "Press Enter to continue\n"
        )
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(adapter._gate_response(screen), "1")

    def test_codex_adapter_keeps_existing_model_on_upgrade_prompt(self) -> None:
        adapter = CodexAdapter(tui_mode=True)
        screen = (
            "Introducing GPT-5.4\n"
            "Choose how you'd like Codex to proceed.\n"
            "1. Try new model\n"
            "2. Use existing model\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(screen))
        self.assertEqual(adapter._gate_response(screen), "2")

    def test_codex_adapter_does_not_false_trigger_on_permission_words_in_output(self) -> None:
        adapter = CodexAdapter(tui_mode=True)
        screen = (
            "› explain the code\n"
            "• The permission model allows writes after explicit approval.\n"
            "• Confirm this by checking the guard clause in the module.\n"
        )
        self.assertFalse(adapter._looks_like_gate_prompt(screen))

    def test_codex_adapter_tui_detects_shell_returned_during_execution(self) -> None:
        call_count = {"n": 0}

        class ExitDuringRunHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("codex"):
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

        adapter = CodexAdapter(tui_mode=True, cli_command="codex", idle_poll_seconds=0.0, idle_after=1)
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
            if argv[2] in {"send-text", "send_text"}:
                return Result("")
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
            if argv[2] in {"send-text", "send_text"}:
                return Result("")
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
            if cmd == "set-option":
                return Result()
            raise AssertionError(f"unexpected tmux command: {argv}")

        from agp.plugins.tmux import TmuxHost
        host = TmuxHost(runner=runner, checkpoint_dir=Path(mkdtemp()))
        session = host.get_or_create_session(agent_id="agt_tmux", workspace_ref="/tmp")
        self.assertEqual(session.session_id, "agp-agt_tmux")
        self.assertTrue(host.session_exists(session))
        health = host.health(session)
        self.assertTrue(health.healthy)

        calls_before = len(calls)
        host.send_text(session, "hello", enter=True)
        send_calls = [c for c in calls[calls_before:] if c[1] == "send-keys"]
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
                if "#{pane_current_path}" in argv:
                    return Result("/tmp/reused-pane\n")
                return Result("0")
            if argv[1] == "capture-pane":
                return Result("existing\n")
            return Result()

        from agp.plugins.tmux import TmuxHost
        host = TmuxHost(runner=runner, checkpoint_dir=Path(mkdtemp()))
        s1 = host.get_or_create_session(agent_id="agt_reuse")
        s2 = host.get_or_create_session(agent_id="agt_reuse")
        self.assertEqual(s1.session_id, s2.session_id)
        self.assertEqual(s2.workspace_ref, "/tmp/reused-pane")

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
                        if cmd == "set-option":
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
                elif "answer the task" in text or "Read the file" in text:
                    self.screen = "\u203a answer the task\n\u2022 tui success\n\u203a \n"

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
            if cmd == "set-option":
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

    # ── Sticky session tests ──────────────────────────────────────────

    def test_codex_sticky_health_check_re_bootstraps_on_crash(self) -> None:
        """When Codex TUI died between jobs, ensure_bootstrapped clears
        the flag and re-launches."""
        tui_alive = {"value": True}

        class WezTermLikeHost(InProcessTerminalHost):
            @property
            def kind(self):
                return "wezterm"

            def is_foreground_tui(self, session):
                return tui_alive["value"]

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        adapter = CodexAdapter(
            tui_mode=True,
            session_mode="sticky",
            idle_poll_seconds=0.0,
            idle_timeout_seconds=0.1,
        )
        host = WezTermLikeHost()
        session = host.get_or_create_session(agent_id="agt_sticky")
        claimed = {
            "agent_id": "agt_sticky",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "first task"},
        }

        # Simulate first bootstrap succeeding — flag gets set
        session.metadata["codex_bootstrapped"] = True

        # TUI is alive — should return early, flag intact
        adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        self.assertTrue(session.metadata.get("codex_bootstrapped"))

        # TUI crashed — flag should be cleared and re-bootstrap attempted
        tui_alive["value"] = False
        try:
            adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        except RecoverableExecutionError:
            pass  # expected — no real TUI to become ready
        # The flag was cleared before re-bootstrap attempt
        history = host._history.get(session.session_id, [])
        self.assertTrue(any("codex" in entry.lower() for entry in history),
                        "should have attempted to re-launch codex")

    def test_codex_sticky_cursor_preserved_across_runs(self) -> None:
        """After a TUI run, cursor should be saved back to session metadata."""
        adapter = CodexAdapter(
            tui_mode=True,
            session_mode="sticky",
            idle_poll_seconds=0.01,
            idle_after=2,
            idle_timeout_seconds=1.0,
        )

        class SequencedVisibleHost(InProcessTerminalHost):
            def __init__(self, screens: list[str]) -> None:
                super().__init__()
                self._screens = list(screens)
                self._last_screen = screens[-1] if screens else ""

            def read_visible(self, session) -> str:  # noqa: ARG002
                if self._screens:
                    self._last_screen = self._screens.pop(0)
                return self._last_screen

        # Sequenced screens: empty baseline, then completed turn after dispatch
        screen_content = (
            "\u203a test task\n"
            "\n"
            "\u2022 The answer is 42\n"
            "\n"
            "\u203a \n"
        )
        host = SequencedVisibleHost(["", screen_content, screen_content])
        session = host.get_or_create_session(agent_id="agt_cursor")

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cursor"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        claimed = {
            "agent_id": "agt_cursor",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "test task"},
        }

        result = adapter.execute_run(
            host=host, session=session, claimed=claimed,
            supervisor=SupervisorStub(),
        )
        self.assertIsNotNone(result)
        # Cursor should be saved back for next run
        self.assertIn("restored_cursor", session.metadata)
        cursor = session.metadata["restored_cursor"]
        self.assertEqual(cursor.session_id, session.session_id)

    def test_codex_sticky_recover_clears_bootstrap_on_exit(self) -> None:
        """recover() should clear codex_bootstrapped when Codex crashed."""
        adapter = CodexAdapter(tui_mode=True, session_mode="sticky")
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_crash")
        session.metadata["codex_bootstrapped"] = True

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_crash"})()
                })()

        adapter.recover(
            host=host,
            session=session,
            claimed={"agent_id": "agt_crash", "job": {"job_id": "j"}, "run": {"run_id": "r"}, "message": {"text": "t"}},
            attempt=1,
            error=PaneDied("codex cli exited during execution"),
            supervisor=SupervisorStub(),
        )
        self.assertNotIn("codex_bootstrapped", session.metadata)

    def test_codex_sticky_recover_keeps_bootstrap_on_other_errors(self) -> None:
        """recover() should NOT clear bootstrap flag for non-exit errors."""
        adapter = CodexAdapter(tui_mode=True, session_mode="sticky")
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_other")
        session.metadata["codex_bootstrapped"] = True

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_other"})()
                })()

        adapter.recover(
            host=host,
            session=session,
            claimed={"agent_id": "agt_other", "job": {"job_id": "j"}, "run": {"run_id": "r"}, "message": {"text": "t"}},
            attempt=1,
            error=RecoverableExecutionError("some other error"),
            supervisor=SupervisorStub(),
        )
        self.assertTrue(session.metadata.get("codex_bootstrapped"))

    def test_codex_sticky_tmux_preserves_session(self) -> None:
        """sticky + tmux should NOT reset the session — TUI stays alive."""

        class TmuxLikeHost(InProcessTerminalHost):
            def __init__(self):
                super().__init__()
                self.reset_called = False

            @property
            def kind(self):
                return "tmux"

            def reset_session(self, session):
                self.reset_called = True
                return super().reset_session(session)

        adapter = CodexAdapter(
            tui_mode=True,
            session_mode="sticky",
            idle_poll_seconds=0.01,
            idle_after=1,
            idle_timeout_seconds=0.5,
        )
        host = TmuxLikeHost()
        session = host.get_or_create_session(agent_id="agt_tmux_sticky")
        session.metadata["codex_bootstrapped"] = True

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_tmux"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        claimed = {
            "agent_id": "agt_tmux_sticky",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "tmux sticky test"},
        }

        try:
            adapter._execute_run_tui(
                host=host, session=session, claimed=claimed,
                supervisor=SupervisorStub(),
            )
        except RecoverableExecutionError:
            pass
        self.assertFalse(host.reset_called, "sticky mode should preserve tmux session")

    def test_codex_ephemeral_resets_on_wezterm(self) -> None:
        """ephemeral + wezterm should reset session before each run."""

        class WezTermLikeHost(InProcessTerminalHost):
            def __init__(self):
                super().__init__()
                self.reset_called = False

            @property
            def kind(self):
                return "wezterm"

            def is_foreground_tui(self, session):
                return True

            def reset_session(self, session):
                self.reset_called = True
                return super().reset_session(session)

        adapter = CodexAdapter(
            tui_mode=True,
            session_mode="ephemeral",
            idle_poll_seconds=0.01,
            idle_after=1,
            idle_timeout_seconds=0.5,
        )
        host = WezTermLikeHost()
        session = host.get_or_create_session(agent_id="agt_wez_eph")

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_wez"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        claimed = {
            "agent_id": "agt_wez_eph",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "ephemeral wezterm test"},
        }

        try:
            adapter._execute_run_tui(
                host=host, session=session, claimed=claimed,
                supervisor=SupervisorStub(),
            )
        except RecoverableExecutionError:
            pass
        self.assertTrue(host.reset_called, "wezterm ephemeral should reset session")

    # ── Claude Code adapter tests ────────────────────────────────────

    def test_claude_code_output_cleaning_extracts_last_response(self) -> None:
        """_clean_claude_code_output should extract the response from TUI chrome."""
        raw = (
            "\u256d\u2500\u2500\u2500 Claude Code v2.1.72 \u2500\u2500\u2500\u256e\n"
            "\u2502  Opus 4.6 \u00b7 Claude Max  \u2502\n"
            "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f\n"
            "\u276f What is 2+2?\n"
            "\u23fa 2 + 2 = 4.\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        )
        cleaned = _clean_claude_code_output(raw)
        self.assertEqual(cleaned, "2 + 2 = 4.")

    def test_claude_code_output_cleaning_strips_feedback_survey(self) -> None:
        """Feedback survey noise should be stripped from cleaned output."""
        raw = (
            "\u276f What is 6 * 7?\n"
            "\u23fa 42\n"
            "\u23fa How is Claude doing this session? (optional)\n"
            "  1: Bad    2: Fine   3: Good   0: Dismiss\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\u23f5\u23f5 bypass permissions on\n"
        )
        cleaned = _clean_claude_code_output(raw)
        self.assertIn("42", cleaned)
        self.assertNotIn("How is Claude doing", cleaned)
        self.assertNotIn("Dismiss", cleaned)

    def test_claude_code_feedback_survey_gate_dismissed(self) -> None:
        """Feedback survey should be auto-dismissed with 0."""
        adapter = ClaudeCodeAdapter()
        survey_screen = (
            "\u23fa How is Claude doing this session? (optional)\n"
            "  1: Bad    2: Fine   3: Good   0: Dismiss\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(survey_screen))
        self.assertFalse(adapter._is_fatal_gate(survey_screen))
        self.assertEqual(adapter._gate_response(survey_screen), "0")

    def test_claude_code_output_cleaning_handles_tool_results(self) -> None:
        raw = (
            "\u276f Read this file\n"
            "\u23fa Let me read the file.\n"
            "  Read src/foo.py\n"
            "  \u23bf file contents here\n"
            "\u23fa The file contains foo.\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
        )
        cleaned = _clean_claude_code_output(raw)
        self.assertIn("The file contains foo.", cleaned)

    def test_claude_code_output_cleaning_handles_compaction(self) -> None:
        raw = (
            "\u276f old question\n"
            "\u23fa old answer\n"
            "\u273b Conversation compacted\n"
            "\u276f new question\n"
            "\u25cf new answer here\n"
            "\u276f \n"
        )
        cleaned = _clean_claude_code_output(raw)
        self.assertEqual(cleaned, "new answer here")

    def test_claude_code_output_cleaning_extracts_trailing_json_for_output_contract_jobs(self) -> None:
        from agp.runtime._types import OutputCursor, OutputReadResult

        class JsonContractHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._visible_reads = 0

            def read_visible(self, session):
                self._visible_reads += 1
                if self._visible_reads == 1:
                    return "\u276f old prompt\n\u23fa OLD_OK\n"
                return (
                    "● Search(pattern: \"dead_lettered_jobs\", path: \"/home/user/projects/skynet/src/agp/queue_backend.py\")\n"
                    "● {\"verdict\": \"approved\", \"summary\": \"looks good\", \"findings\": []}\n"
                    "\n"
                    "\u273b Churned for 1m 40s\n"
                    "\n"
                    "\u276f \n"
                    "\u2500\u2500\u2500\u2500\n"
                )

            def read_output(self, session, cursor):
                full_text = (
                    "\u276f Review the fix\n"
                    "● Search(pattern: \"dead_lettered_jobs\", path: \"/home/user/projects/skynet/src/agp/queue_backend.py\")\n"
                    "● {\"verdict\": \"approved\", \"summary\": \"looks good\", \"findings\": []}\n"
                    "\n"
                    "\u273b Churned for 1m 40s\n"
                    "\n"
                )
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=OutputCursor(session_id=session.session_id, metadata={"read": 1}),
                    text=full_text,
                    full_text=full_text,
                    changed=True,
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_claude_json_contract"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = JsonContractHost()
        session = host.get_or_create_session(agent_id="agt_claude_json_contract")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_claude_json_contract",
            "job": {
                "job_id": "job_claude_json_contract",
                "output_contract_json": {
                    "format": "json",
                    "json_schema": {
                        "type": "object",
                        "required": ["verdict", "summary"],
                    },
                },
            },
            "run": {"run_id": "run_claude_json_contract"},
            "message": {"text": "Review the fix"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_log = next(artifact for artifact in result.artifacts if artifact.name == "result.txt")
        self.assertEqual(result_log.content, '{"verdict":"approved","summary":"looks good","findings":[]}')

    def test_claude_code_ready_detection(self) -> None:
        adapter = ClaudeCodeAdapter()
        # Ready: has ❯ prompt and separator
        ready_screen = (
            "\u256d\u2500 Claude Code \u2500\u256e\n"
            "\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u256f\n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        )
        self.assertTrue(adapter._looks_like_ready(ready_screen))
        # Not ready: just a shell with ❯
        shell_screen = "\u276f ls\nfile1 file2\n\u276f \n"
        self.assertFalse(adapter._looks_like_ready(shell_screen))

    def test_claude_code_gate_detection_first_run_screens(self) -> None:
        """Gate patterns detect and correctly classify all first-run screens."""
        adapter = ClaudeCodeAdapter()

        # ── Auto-dismiss gates ────────────────────────────────────────

        # Theme picker
        theme_screen = (
            "Welcome to Claude Code v2.1.81\n"
            "Let's get started.\n"
            "Choose the text style that looks best with your terminal\n"
            "To change this later, run /theme\n"
            "\u276f 1. Dark mode \u2714\n"
            "  2. Light mode\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(theme_screen))
        self.assertFalse(adapter._is_fatal_gate(theme_screen))
        self.assertEqual(adapter._gate_response(theme_screen), "")
        self.assertFalse(adapter._looks_like_ready(theme_screen))

        # Syntax highlighting preview
        highlight_screen = "Syntax highlighting available only in native build\n"
        self.assertTrue(adapter._looks_like_gate_prompt(highlight_screen))
        self.assertFalse(adapter._is_fatal_gate(highlight_screen))

        # Login success confirmation
        login_success = (
            "Logged in as user@example.com\n"
            "Login successful. Press Enter to continue\u2026\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(login_success))
        self.assertFalse(adapter._is_fatal_gate(login_success))
        self.assertEqual(adapter._gate_response(login_success), "")

        # Security notes
        security_screen = (
            "Security notes:\n"
            "1. Claude can make mistakes\n"
            "2. Due to prompt injection risks\n"
            "Press Enter to continue\u2026\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(security_screen))
        self.assertFalse(adapter._is_fatal_gate(security_screen))
        self.assertEqual(adapter._gate_response(security_screen), "")

        # Bypass permissions warning
        bypass_screen = (
            "WARNING: Claude Code running in Bypass Permissions mode\n"
            "By proceeding, you accept all responsibility for actions taken.\n"
            "\u276f 1. No, exit\n"
            "  2. Yes, I accept\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(bypass_screen))
        self.assertFalse(adapter._is_fatal_gate(bypass_screen))
        self.assertEqual(adapter._gate_response(bypass_screen), "2")

        # Workspace trust (quick safety check variant)
        trust_screen = (
            "Accessing workspace:\n/app\n"
            "Quick safety check: Is this a project you trust?\n"
            "\u276f 1. Yes, I trust this folder\n"
            "  2. No, exit\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(trust_screen))
        self.assertFalse(adapter._is_fatal_gate(trust_screen))
        self.assertEqual(adapter._gate_response(trust_screen), "1")

        # Trust prompt (alternate phrasing)
        trust_alt = "Do you trust the contents of /workspace?\n1. I trust this folder\n"
        self.assertTrue(adapter._looks_like_gate_prompt(trust_alt))
        self.assertEqual(adapter._gate_response(trust_alt), "1")

        # ── Fatal gates (require user action) ────────────────────────

        # Login method selection
        login_screen = (
            "Select login method:\n"
            "\u276f 1. Claude account with subscription\n"
            "  2. Anthropic Console account\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(login_screen))
        self.assertTrue(adapter._is_fatal_gate(login_screen))

        # OAuth paste code prompt
        oauth_screen = (
            "Browser didn't open? Use the url below to sign in\n"
            "https://claude.ai/oauth/authorize?...\n"
            "Paste code here if prompted >\n"
        )
        self.assertTrue(adapter._looks_like_gate_prompt(oauth_screen))
        self.assertTrue(adapter._is_fatal_gate(oauth_screen))

        # OAuth error
        oauth_error = "OAuth error: Invalid code.\nPress Enter to retry.\n"
        self.assertTrue(adapter._looks_like_gate_prompt(oauth_error))
        self.assertTrue(adapter._is_fatal_gate(oauth_error))

    def test_claude_code_shell_returned_detection(self) -> None:
        adapter = ClaudeCodeAdapter()
        # Shell returned (no TUI indicators)
        self.assertTrue(adapter._looks_like_shell_returned("$ \nsome output\n$ "))
        # TUI still running (has separator)
        tui_screen = "\u23fa response\n\u2500\u2500\u2500\u2500\n\u276f \n"
        self.assertFalse(adapter._looks_like_shell_returned(tui_screen))

    def test_claude_code_tui_shell_return_after_completed_turn_succeeds(self) -> None:
        """When Claude Code finishes and the shell returns, the adapter should
        succeed via last_good_screen instead of raising PaneDied."""
        from agp.runtime._types import OutputCursor, OutputReadResult

        class ClaudeOneShotHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._phase = "bootstrap"  # bootstrap → response → shell
                self._response_reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if self._phase == "bootstrap" and ("task prompt" in text or "Read the file" in text):
                    self._phase = "response"
                    self._history.setdefault(session.session_id, []).append(
                        "\u276f task prompt\n"
                        "\u23fa Here is the answer.\n"
                        "\u2500\u2500\u2500\u2500\n"
                        "\u276f \n"
                    )

            def read_visible(self, session):
                if self._phase == "bootstrap":
                    # Idle Claude TUI for bootstrap + baseline capture
                    return "\u276f \n\u2500\u2500\u2500\u2500\n"
                if self._phase == "response":
                    self._response_reads += 1
                    if self._response_reads > 3:
                        self._phase = "shell"
                        return "$ \n"
                    return (
                        "\u276f task prompt\n"
                        "\u23fa Here is the answer.\n"
                        "\u2500\u2500\u2500\u2500\n"
                        "\u276f \n"
                    )
                return "$ \n"

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_cc_oneshot"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = ClaudeOneShotHost()
        session = host.get_or_create_session(agent_id="agt_cc_oneshot")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_cc_oneshot",
            "job": {"job_id": "job_cc_oneshot"},
            "run": {"run_id": "run_cc_oneshot"},
            "message": {"text": "task prompt"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_log = next(a for a in result.artifacts if a.name == "result.txt")
        self.assertIn("Here is the answer", result_log.content)

    def test_claude_code_tui_last_good_screen_fallback_on_tui_exit_race(self) -> None:
        """When read_visible returns empty after the loop (TUI exited),
        last_good_screen should provide the result."""
        from agp.runtime._types import OutputCursor, OutputReadResult

        class ClaudeExitRaceHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._phase = "bootstrap"
                self._response_reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if self._phase == "bootstrap" and ("task prompt" in text or "Read the file" in text):
                    self._phase = "response"
                    self._history.setdefault(session.session_id, []).append(
                        "\u276f task prompt\n"
                        "\u23fa Race condition result.\n"
                        "\u2500\u2500\u2500\u2500\n"
                        "\u276f \n"
                    )

            def read_visible(self, session):
                if self._phase == "bootstrap":
                    return "\u276f \n\u2500\u2500\u2500\u2500\n"
                if self._phase == "response":
                    self._response_reads += 1
                    if self._response_reads > 3:
                        self._phase = "exited"
                        return ""  # TUI exited — blank screen (the race)
                    return (
                        "\u276f task prompt\n"
                        "\u23fa Race condition result.\n"
                        "\u2500\u2500\u2500\u2500\n"
                        "\u276f \n"
                    )
                return ""  # blank after exit

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_cc_race"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = ClaudeExitRaceHost()
        session = host.get_or_create_session(agent_id="agt_cc_race")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_cc_race",
            "job": {"job_id": "job_cc_race"},
            "run": {"run_id": "run_cc_race"},
            "message": {"text": "task prompt"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_log = next(a for a in result.artifacts if a.name == "result.txt")
        self.assertIn("Race condition result", result_log.content)

    def test_claude_code_does_not_complete_on_prompt_only_screen_before_answer(self) -> None:
        class ClaudePromptRaceHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._phase = "bootstrap"
                self._prompt_only_reads = 0

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                # Via-file: the adapter sends a reference string, not the raw prompt
                if self._phase == "bootstrap" and ("task prompt" in text or "Read the file" in text):
                    self._phase = "prompt_only"

            def read_visible(self, session):
                if self._phase == "bootstrap":
                    return "\u276f \n\u2500\u2500\u2500\u2500\n"
                if self._phase == "prompt_only":
                    self._prompt_only_reads += 1
                    if self._prompt_only_reads > 3:
                        self._phase = "response"
                    return "\u276f task prompt\n"
                return (
                    "\u276f task prompt\n"
                    "\u23fa The real answer is 4.\n"
                    "\u2500\u2500\u2500\u2500\n"
                    "\u276f \n"
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_cc_prompt_race"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.1,
        )
        host = ClaudePromptRaceHost()
        session = host.get_or_create_session(agent_id="agt_cc_prompt_race")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_cc_prompt_race",
            "job": {"job_id": "job_cc_prompt_race"},
            "run": {"run_id": "run_cc_prompt_race"},
            "message": {"text": "task prompt"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_log = next(a for a in result.artifacts if a.name == "result.txt")
        self.assertIn("The real answer is 4", result_log.content)

    def test_claude_code_bootstrap_verifies_health(self) -> None:
        from agp.runtime import SessionHealth

        class UnhealthyHost(InProcessTerminalHost):
            def health(self, session):
                return SessionHealth(
                    session_id=session.session_id,
                    exists=False,
                    healthy=False,
                    reason="pane_dead",
                )

        adapter = ClaudeCodeAdapter()
        host = UnhealthyHost()
        session = host.get_or_create_session(agent_id="agt_cc_health")
        claimed = {"agent_id": "agt_cc_health", "job": {"job_id": "j"}, "run": {"run_id": "r"}, "message": {"text": "t"}}
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        self.assertIn("unhealthy before bootstrap", str(ctx.exception))

    def test_claude_code_sticky_health_check_re_bootstraps_on_crash(self) -> None:
        tui_alive = {"value": True}

        class WezTermLikeHost(InProcessTerminalHost):
            @property
            def kind(self):
                return "wezterm"

            def is_foreground_tui(self, session):
                return tui_alive["value"]

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.0,
            idle_timeout_seconds=0.1,
        )
        host = WezTermLikeHost()
        session = host.get_or_create_session(agent_id="agt_cc_sticky")
        claimed = {"agent_id": "agt_cc_sticky", "job": {"job_id": "j1"}, "run": {"run_id": "r1"}, "message": {"text": "task"}}

        session.metadata["claude_code_bootstrapped"] = True
        adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        self.assertTrue(session.metadata.get("claude_code_bootstrapped"))

        # TUI crashed
        tui_alive["value"] = False
        try:
            adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        except RecoverableExecutionError:
            pass
        history = host._history.get(session.session_id, [])
        self.assertTrue(any("claude" in entry.lower() for entry in history),
                        "should have attempted to re-launch claude code")

    def test_claude_code_sticky_cursor_preserved_across_runs(self) -> None:
        class SequencedVisibleHost(InProcessTerminalHost):
            def __init__(self, screens: list[str]) -> None:
                super().__init__()
                self._screens = list(screens)
                self._last_screen = screens[-1] if screens else ""

            def read_visible(self, session) -> str:  # noqa: ARG002
                if self._screens:
                    self._last_screen = self._screens.pop(0)
                return self._last_screen

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.01,
            idle_after=1,
            idle_timeout_seconds=1.0,
        )
        screen_content = (
            "\u276f test task\n"
            "\u23fa The answer is 42\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\n"
        )
        host = SequencedVisibleHost(["", screen_content, screen_content])
        session = host.get_or_create_session(agent_id="agt_cc_cursor")

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_cursor"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        claimed = {
            "agent_id": "agt_cc_cursor",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "test task"},
        }
        result = adapter.execute_run(
            host=host, session=session, claimed=claimed,
            supervisor=SupervisorStub(),
        )
        self.assertIsNotNone(result)
        self.assertIn("restored_cursor", session.metadata)

    def test_claude_code_tui_waits_for_stable_non_working_screen_before_completion(self) -> None:
        class SequencedVisibleHost(InProcessTerminalHost):
            def __init__(self, screens: list[str]) -> None:
                super().__init__()
                self._screens = list(screens)
                self._last_screen = screens[-1] if screens else ""

            def read_visible(self, session) -> str:  # noqa: ARG002
                if self._screens:
                    self._last_screen = self._screens.pop(0)
                return self._last_screen

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_stable"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.2,
        )
        working_screen = (
            "\u276f test task\n"
            "\u25cf Let me think this through.\n"
            "\u2234 Thinking...\n"
            "\u2500\u2500\u2500\u2500\n"
        )
        completed_screen = (
            "\u276f test task\n"
            "\u25cf The answer is 42\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\n"
        )
        host = SequencedVisibleHost(["", working_screen, working_screen, completed_screen, completed_screen])
        session = host.get_or_create_session(agent_id="agt_cc_stable")

        claimed = {
            "agent_id": "agt_cc_stable",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "test task"},
        }
        result = adapter.execute_run(
            host=host, session=session, claimed=claimed,
            supervisor=SupervisorStub(),
        )
        self.assertEqual(result.artifacts[-1].content, "The answer is 42")

    def test_claude_code_tui_completes_when_stale_thinking_line_remains_visible(self) -> None:
        class SequencedVisibleHost(InProcessTerminalHost):
            def __init__(self, screens: list[str]) -> None:
                super().__init__()
                self._screens = list(screens)
                self._last_screen = screens[-1] if screens else ""

            def read_visible(self, session) -> str:  # noqa: ARG002
                if self._screens:
                    self._last_screen = self._screens.pop(0)
                return self._last_screen

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_stale"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.2,
        )
        completed_screen = (
            "\u276f Reply with exactly: claude-dev-ok\n"
            "\u2234 Thinking\u2026\n"
            "  The user is asking me to reply with exactly \"claude-dev-ok\".\n"
            "\u25cf claude-dev-ok\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\n"
            "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)   22466 tokens\n"
        )
        host = SequencedVisibleHost(["", completed_screen, completed_screen, completed_screen])
        session = host.get_or_create_session(agent_id="agt_cc_stale")

        claimed = {
            "agent_id": "agt_cc_stale",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "Reply with exactly: claude-dev-ok"},
        }
        result = adapter.execute_run(
            host=host, session=session, claimed=claimed,
            supervisor=SupervisorStub(),
        )
        self.assertEqual(result.artifacts[-1].content, "claude-dev-ok")

    def test_claude_code_tui_raises_stable_but_indeterminate_for_stuck_dialog(self) -> None:
        from agp.runtime import OutputReadResult, StableButIndeterminate

        class StableDialogHost(InProcessTerminalHost):
            def __init__(self, screen: str) -> None:
                super().__init__()
                self._screen = screen
                self._dispatched = False

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                self._dispatched = True

            def read_visible(self, session) -> str:  # noqa: ARG002
                if not self._dispatched:
                    return ""
                return self._screen

            def read_output(self, session, cursor):
                full_text = self._screen if self._dispatched else ""
                prior = cursor.checkpoint
                if full_text.startswith(prior):
                    delta = full_text[len(prior):]
                else:
                    delta = full_text
                updated = OutputCursor(
                    session_id=session.session_id,
                    checkpoint=full_text,
                    metadata=dict(cursor.metadata),
                )
                return OutputReadResult(
                    session_id=session.session_id,
                    cursor=updated,
                    text=delta,
                    full_text=full_text,
                    changed=bool(delta),
                )

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_indeterminate"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.01,
            idle_after=2,
            idle_timeout_seconds=0.2,
        )
        stuck_dialog_screen = (
            "Permission review required\n"
            "This dialog is stuck and shows content, but there is no prompt.\n"
        )
        host = StableDialogHost(stuck_dialog_screen)
        session = host.get_or_create_session(agent_id="agt_cc_indeterminate")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_cc_indeterminate",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "test task"},
        }

        with self.assertRaises(StableButIndeterminate) as ctx:
            adapter.execute_run(
                host=host, session=session, claimed=claimed,
                supervisor=SupervisorStub(),
            )

        self.assertTrue(ctx.exception.screen)

    def test_claude_code_recover_clears_bootstrap_on_exit(self) -> None:
        adapter = ClaudeCodeAdapter(session_mode="sticky")
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_cc_crash")
        session.metadata["claude_code_bootstrapped"] = True

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_crash"})()
                })()

        adapter.recover(
            host=host, session=session,
            claimed={"agent_id": "agt_cc_crash", "job": {"job_id": "j"}, "run": {"run_id": "r"}, "message": {"text": "t"}},
            attempt=1,
            error=PaneDied("claude code cli exited during execution"),
            supervisor=SupervisorStub(),
        )
        self.assertNotIn("claude_code_bootstrapped", session.metadata)

    def test_claude_code_recover_keeps_bootstrap_on_other_errors(self) -> None:
        adapter = ClaudeCodeAdapter(session_mode="sticky")
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_cc_other")
        session.metadata["claude_code_bootstrapped"] = True

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_other"})()
                })()

        adapter.recover(
            host=host, session=session,
            claimed={"agent_id": "agt_cc_other", "job": {"job_id": "j"}, "run": {"run_id": "r"}, "message": {"text": "t"}},
            attempt=1,
            error=RecoverableExecutionError("some other error"),
            supervisor=SupervisorStub(),
        )
        self.assertTrue(session.metadata.get("claude_code_bootstrapped"))

    def test_claude_code_tmux_uses_tui_mode(self) -> None:
        """On tmux, Claude Code adapter uses TUI mode (not oneshot)."""
        class SequencedVisibleHost(InProcessTerminalHost):
            def __init__(self, screens: list[str]) -> None:
                super().__init__()
                self._screens = list(screens)
                self._last_screen = screens[-1] if screens else ""

            def read_visible(self, session) -> str:  # noqa: ARG002
                if self._screens:
                    self._last_screen = self._screens.pop(0)
                return self._last_screen

        adapter = ClaudeCodeAdapter(
            session_mode="sticky",
            idle_poll_seconds=0.01,
            idle_after=1,
            idle_timeout_seconds=1.0,
        )
        screen_content = (
            "\u276f What is 2+2?\n"
            "\u23fa 4\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\n"
        )
        host = SequencedVisibleHost(["", screen_content, screen_content])
        session = host.get_or_create_session(agent_id="agt_cc_tmux")

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_cc_tmux"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        claimed = {
            "agent_id": "agt_cc_tmux",
            "job": {"job_id": "j1"},
            "run": {"run_id": "r1"},
            "message": {"text": "What is 2+2?"},
        }
        result = adapter.execute_run(
            host=host, session=session, claimed=claimed,
            supervisor=SupervisorStub(),
        )
        self.assertEqual(result.summary["mode"], "tui")

    def test_wezterm_is_foreground_tui_detects_bash_prompt_with_trailing_dollar(self) -> None:
        """A bash prompt like 'user@host:~$' must be detected as shell, not TUI."""

        def runner(argv: list[str], input: str | None = None, **_: object):
            if argv[2] == "get-text":
                return type("R", (), {"stdout": "agpuser@agp-runtime:~$ \n", "stderr": "", "returncode": 0})()
            if argv[2] == "list":
                return type("R", (), {
                    "stdout": json.dumps([{
                        "pane_id": 99, "tab_id": 1, "window_id": 1,
                        "workspace": "agp-test", "window_title": "AGP:agt_shell",
                        "tab_title": "AGP:agt_shell", "cwd": "/home/agpuser",
                    }]),
                    "stderr": "", "returncode": 0,
                })()
            if argv[2] in {"send-text", "send_text"}:
                return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
            raise AssertionError(f"unexpected: {argv}")

        host = WezTermHost(workspace="agp-test", runner=runner)
        session = host.get_or_create_session(agent_id="agt_shell")
        self.assertFalse(host.is_foreground_tui(session))

    def test_tmux_is_foreground_tui_detects_shell_prompt(self) -> None:
        from agp.plugins.tmux import TmuxHost
        from agp.runtime import TerminalSession

        def runner(argv: list[str], **_: object):
            if argv[1:] == ["capture-pane", "-t", "agp-shell", "-p", "-S", "-50"]:
                return type("R", (), {"stdout": "agpuser@agp-runtime:~$ \n", "stderr": "", "returncode": 0})()
            raise AssertionError(f"unexpected: {argv}")

        host = TmuxHost(runner=runner)
        session = TerminalSession(session_id="agp-shell", agent_id="agt_shell")
        self.assertFalse(host.is_foreground_tui(session))

    def test_tmux_is_foreground_tui_detects_tui_markers(self) -> None:
        from agp.plugins.tmux import TmuxHost
        from agp.runtime import TerminalSession

        screen = (
            "\u276f What is 2+2?\n"
            "\u23fa 4\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\n"
        )

        def runner(argv: list[str], **_: object):
            if argv[1:] == ["capture-pane", "-t", "agp-tui", "-p", "-S", "-50"]:
                return type("R", (), {"stdout": screen, "stderr": "", "returncode": 0})()
            raise AssertionError(f"unexpected: {argv}")

        host = TmuxHost(runner=runner)
        session = TerminalSession(session_id="agp-tui", agent_id="agt_tui")
        self.assertTrue(host.is_foreground_tui(session))

    def test_tmux_is_foreground_tui_defaults_true_when_ambiguous(self) -> None:
        from agp.plugins.tmux import TmuxHost
        from agp.runtime import TerminalSession

        def runner(argv: list[str], **_: object):
            if argv[1:] == ["capture-pane", "-t", "agp-ambiguous", "-p", "-S", "-50"]:
                return type("R", (), {"stdout": "running command\ncollecting output\n", "stderr": "", "returncode": 0})()
            raise AssertionError(f"unexpected: {argv}")

        host = TmuxHost(runner=runner)
        session = TerminalSession(session_id="agp-ambiguous", agent_id="agt_ambiguous")
        self.assertTrue(host.is_foreground_tui(session))

    def test_tmux_is_foreground_tui_defaults_true_when_capture_pane_fails(self) -> None:
        from agp.plugins.tmux import TmuxHost
        from agp.runtime import TerminalSession

        def runner(argv: list[str], **_: object):
            if argv[1:] == ["capture-pane", "-t", "agp-fail", "-p", "-S", "-50"]:
                return type("R", (), {"stdout": "", "stderr": "server exited unexpectedly", "returncode": 1})()
            raise AssertionError(f"unexpected: {argv}")

        host = TmuxHost(runner=runner)
        session = TerminalSession(session_id="agp-fail", agent_id="agt_fail")
        with self.assertLogs("agp.plugins.tmux", level="WARNING") as logs:
            self.assertTrue(host.is_foreground_tui(session))
        self.assertTrue(any("capture-pane failed" in line for line in logs.output))

    def test_tmux_is_foreground_tui_empty_screen_shell_idle_returns_false(self) -> None:
        """Empty screen + shell is idle → TUI is not running."""
        from agp.plugins.tmux import TmuxHost
        from agp.runtime import TerminalSession

        def runner(argv: list[str], **_: object):
            if argv[1:] == ["capture-pane", "-t", "agp-empty", "-p", "-S", "-50"]:
                return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
            if "display-message" in argv:
                return type("R", (), {"stdout": "/dev/ttys099\n", "stderr": "", "returncode": 0})()
            raise AssertionError(f"unexpected: {argv}")

        import subprocess as _sp
        orig_run = _sp.run
        def patched_run(cmd, **kw):
            # ps query for shell_idle — simulate idle shell
            if cmd and cmd[0] == "ps":
                return type("R", (), {"stdout": "Ss+  -zsh\n", "stderr": "", "returncode": 0})()
            return orig_run(cmd, **kw)

        from unittest.mock import patch
        with patch("subprocess.run", side_effect=patched_run):
            host = TmuxHost(runner=runner)
            session = TerminalSession(session_id="agp-empty", agent_id="agt_empty")
            self.assertFalse(host.is_foreground_tui(session))

    def test_tmux_is_foreground_tui_empty_screen_tui_process_returns_true(self) -> None:
        """Empty screen + known TUI process (claude) running → TUI alive."""
        from agp.plugins.tmux import TmuxHost
        from agp.runtime import TerminalSession

        def runner(argv: list[str], **_: object):
            if argv[1:] == ["capture-pane", "-t", "agp-redraw", "-p", "-S", "-50"]:
                return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
            if "display-message" in argv:
                return type("R", (), {"stdout": "/dev/ttys099\n", "stderr": "", "returncode": 0})()
            raise AssertionError(f"unexpected: {argv}")

        import subprocess as _sp
        orig_run = _sp.run
        def patched_run(cmd, **kw):
            # ps query — simulate claude as foreground process
            if cmd and cmd[0] == "ps":
                return type("R", (), {"stdout": "Ss+  -zsh\nS+   claude\n", "stderr": "", "returncode": 0})()
            return orig_run(cmd, **kw)

        from unittest.mock import patch
        with patch("subprocess.run", side_effect=patched_run):
            host = TmuxHost(runner=runner)
            session = TerminalSession(session_id="agp-redraw", agent_id="agt_redraw")
            self.assertTrue(host.is_foreground_tui(session))

    def test_build_agent_adapter_claude_code(self) -> None:
        """build_agent_adapter should return ClaudeCodeAdapter for kind='claude_code'."""
        adapter = build_agent_adapter("claude_code")
        self.assertEqual(adapter.kind, "claude_code")
        self.assertIsInstance(adapter, ClaudeCodeAdapter)

    # ── StableButIndeterminate tests ────────────────────────────────────

    def test_claude_code_stable_but_indeterminate_on_stuck_dialog(self) -> None:
        """Non-empty stable screen with no prompt raises StableButIndeterminate."""
        from agp.runtime import StableButIndeterminate

        class DialogHost(InProcessTerminalHost):
            """Host that shows a stuck dialog screen (no ❯ prompt)."""
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                # Simulate a permission dialog appearing
                self._history.setdefault(session.session_id, []).append(
                    "Do you trust this folder?\n1) Yes\n2) No\n"
                )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_sbi"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {}

        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.01,
            idle_after=2,
            idle_timeout_seconds=2.0,
        )
        host = DialogHost()
        session = host.get_or_create_session(agent_id="agt_sbi")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_sbi",
            "job": {"job_id": "job_sbi"},
            "run": {"run_id": "run_sbi"},
            "message": {"text": "test task"},
        }
        with self.assertRaises(StableButIndeterminate) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertTrue(ctx.exception.screen.strip(), "screen should be non-empty")
        self.assertIn("cannot determine", str(ctx.exception))

    def test_claude_code_empty_screen_raises_indeterminate(self) -> None:
        """Empty screen should raise StableButIndeterminate quickly."""
        class SilentHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                pass

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_empty_cc"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {}

        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.01,
            idle_after=1,
            idle_timeout_seconds=0.5,
        )
        host = SilentHost()
        session = host.get_or_create_session(agent_id="agt_empty_cc")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_empty_cc",
            "job": {"job_id": "job_empty_cc"},
            "run": {"run_id": "run_empty_cc"},
            "message": {"text": "silent"},
        }
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("stable but adapter cannot determine", str(ctx.exception))

    def test_claude_code_long_response_extracts_from_scrollback(self) -> None:
        """When ⏺ markers scroll off the visible screen, extraction should
        succeed via the scrollback (full tmux buffer) fallback."""
        from agp.plugins.claude_code import ClaudeCodeAdapter

        # The full response (visible in scrollback but NOT on screen)
        FULL_RESPONSE = (
            "\u276f review the adapter\n"
            "\u23fa Here is my detailed review of the adapter code.\n"
            "\n"
            "  Bug 1: The extraction cascade is fragile because...\n"
            "  Bug 2: The poll loop has several issues including...\n"
            "  Bug 3: Gate dismissals don't extend the deadline...\n"
            "\n"
            "  session.metadata[\"restored_cursor\"] = read.cursor\n"
            "\n"
            "  Impact: Silent data corruption.\n"
            "\n"
            "\u2733 Worked for 41s\n"
            "\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
        )

        # Only the tail is visible (no ⏺ marker, no ❯ with prompt text)
        VISIBLE_TAIL = (
            "  session.metadata[\"restored_cursor\"] = read.cursor\n"
            "\n"
            "  Impact: Silent data corruption.\n"
            "\n"
            "\u2733 Worked for 41s\n"
            "\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
            "\u2500\u2500\u2500\u2500\n"
            "  sTAT | Opus 4.6 (1M context)\n"
            "  \u23f5\u23f5 bypass permissions on\n"
        )

        class LongResponseHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self):
                super().__init__()
                self._phase = "bootstrap"
                self._response_reads = 0

            def send_text(self, session, text, *, enter=True):
                super().send_text(session, text, enter=enter)
                if self._phase == "bootstrap" and ("review the adapter" in text or "Read the file" in text):
                    self._phase = "response"

            def read_visible(self, session):
                if self._phase == "bootstrap":
                    return "\u276f \n\u2500\u2500\u2500\u2500\n"
                # During polling: return the visible tail (no ⏺ marker)
                self._response_reads += 1
                return VISIBLE_TAIL

            def read_scrollback(self, session):
                if self._phase == "bootstrap":
                    return self.read_visible(session)
                # Scrollback has the FULL response with ⏺ markers
                return FULL_RESPONSE

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_long"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.5,
        )
        host = LongResponseHost()
        session = host.get_or_create_session(agent_id="agt_long")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_long",
            "job": {"job_id": "job_long"},
            "run": {"run_id": "run_long"},
            "message": {"text": "review the adapter"},
        }

        result = adapter.execute_run(
            host=host, session=session, claimed=claimed, supervisor=SupervisorStub(),
        )
        result_log = next(a for a in result.artifacts if a.name == "result.txt")
        # Must extract the full review from scrollback, not garbage from visible
        self.assertIn("detailed review of the adapter", result_log.content)
        self.assertIn("Bug 1", result_log.content)
        self.assertIn("Bug 3", result_log.content)
        # Must NOT have extracted the quoted code as the response
        self.assertNotEqual(result_log.content.strip(), '["restored_cursor"]')

    def test_claude_code_visible_fast_path_when_markers_present(self) -> None:
        """When ⏺ markers ARE visible, extraction should still work (fast path)."""
        from agp.plugins.claude_code import ClaudeCodeAdapter

        SCREEN = (
            "\u276f hello\n"
            "\u23fa Hi there! How can I help?\n"
            "\u2500\u2500\u2500\u2500\n"
            "\u276f \n"
        )

        class ShortResponseHost(InProcessTerminalHost):
            @property
            def kind(self):
                return "tmux"

            def __init__(self):
                super().__init__()
                self._phase = "bootstrap"
                self._reads = 0

            def send_text(self, session, text, *, enter=True):
                super().send_text(session, text, enter=enter)
                if self._phase == "bootstrap" and ("hello" in text or "Read the file" in text):
                    self._phase = "response"

            def read_visible(self, session):
                if self._phase == "bootstrap":
                    return "\u276f \n\u2500\u2500\u2500\u2500\n"
                self._reads += 1
                return SCREEN

            def read_scrollback(self, session):
                return self.read_visible(session)

        class SupervisorStub:
            def __init__(self):
                self.client = type("Client", (), {
                    "identity": type("Identity", (), {"runtime_id": "rtm_short"})()
                })()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = ClaudeCodeAdapter(
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=0.5,
        )
        host = ShortResponseHost()
        session = host.get_or_create_session(agent_id="agt_short")
        session.metadata["claude_code_bootstrapped"] = True
        claimed = {
            "agent_id": "agt_short",
            "job": {"job_id": "job_short"},
            "run": {"run_id": "run_short"},
            "message": {"text": "hello"},
        }

        result = adapter.execute_run(
            host=host, session=session, claimed=claimed, supervisor=SupervisorStub(),
        )
        result_log = next(a for a in result.artifacts if a.name == "result.txt")
        self.assertIn("Hi there", result_log.content)

    # ── Codex ephemeral tmux output-schema tests ────────────────────────

    def test_codex_ephemeral_tmux_contract_uses_exec_with_output_schema(self) -> None:
        """For JSON contract jobs on ephemeral tmux, launch_command should use
        'codex exec --output-schema <file>' instead of plain 'codex'."""

        class TmuxCapture(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self.launched_commands: list[str] = []

            def reset_session(self, session):
                return super().reset_session(session)

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.launched_commands.append(command)
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "codex" in text:
                    response = json.dumps({"verdict": "approved", "summary": "ok"})
                    self._history.setdefault(session.session_id, []).append(
                        f"\u203a task\n\u2022 {response}\n\n\u203a \n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_schema"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
            session_mode="ephemeral",
        )
        host = TmuxCapture()
        session = host.get_or_create_session(agent_id="agt_schema")

        contract = {
            "format": "json",
            "json_schema": {
                "type": "object",
                "required": ["verdict", "summary"],
                "properties": {
                    "verdict": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        }
        claimed = {
            "agent_id": "agt_schema",
            "job": {"job_id": "job_schema", "output_contract_json": contract},
            "run": {"run_id": "run_schema_test"},
            "message": {"text": "review this code"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())

        # (1) Verify 'codex exec' was used, not plain 'codex'
        self.assertTrue(host.launched_commands, "expected at least one launch_command call")
        cmd = host.launched_commands[-1]
        self.assertIn("exec", cmd, f"expected 'exec' in command: {cmd}")
        self.assertIn("--output-schema", cmd, f"expected '--output-schema' in command: {cmd}")

        # (2) The schema file should contain the json_schema from the contract
        # Extract the schema file path from the command
        parts = shlex.split(cmd)
        schema_idx = parts.index("--output-schema")
        schema_path = parts[schema_idx + 1]
        # Schema file is cleaned up in execute_run's finally block, so we verify
        # indirectly: the path pattern must match the expected format
        self.assertIn("agp-schema-run_schema_test", schema_path)

        # (3) Result should contain the structured JSON
        result_artifact = next(a for a in result.artifacts if a.role == "result")
        parsed = json.loads(result_artifact.content)
        self.assertEqual(parsed["verdict"], "approved")

        # (4) Schema file should be cleaned up
        from pathlib import Path
        self.assertFalse(Path(schema_path).exists(), "schema file should be cleaned up after execute_run")

    def test_codex_ephemeral_tmux_no_contract_uses_plain_codex(self) -> None:
        """Without an output contract, ephemeral tmux should launch plain 'codex'
        (no exec, no --output-schema)."""

        class TmuxCapture(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self.launched_commands: list[str] = []

            def reset_session(self, session):
                return super().reset_session(session)

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.launched_commands.append(command)
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "codex" in text:
                    self._history.setdefault(session.session_id, []).append(
                        "\u203a task\n\u2022 done\n\n\u203a \n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_no_schema"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
            session_mode="ephemeral",
        )
        host = TmuxCapture()
        session = host.get_or_create_session(agent_id="agt_no_schema")

        claimed = {
            "agent_id": "agt_no_schema",
            "job": {"job_id": "job_no_schema"},
            "run": {"run_id": "run_no_schema_test"},
            "message": {"text": "What is 2 + 2?"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())

        # Plain codex: no 'exec', no '--output-schema'
        self.assertTrue(host.launched_commands, "expected at least one launch_command call")
        cmd = host.launched_commands[-1]
        self.assertNotIn("exec", cmd, f"plain codex should not use 'exec': {cmd}")
        self.assertNotIn("--output-schema", cmd, f"plain codex should not use '--output-schema': {cmd}")
        self.assertIn("codex --full-auto", cmd)

    def test_codex_ephemeral_tmux_schema_file_written_with_correct_content(self) -> None:
        """Verify the schema file contents match the job's json_schema before cleanup."""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch

        captured_schema_content: list[str] = []

        class TmuxSchemaCapture(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()

            def reset_session(self, session):
                return super().reset_session(session)

            def launch_command(self, session, *, command, env=None, cwd=None):
                # Read the schema file before the finally block cleans it up
                parts = shlex.split(command)
                if "--output-schema" in parts:
                    idx = parts.index("--output-schema")
                    schema_path = parts[idx + 1]
                    content = Path(schema_path).read_text(encoding="utf-8")
                    captured_schema_content.append(content)
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "codex" in text:
                    response = json.dumps({"verdict": "approved", "summary": "lgtm"})
                    self._history.setdefault(session.session_id, []).append(
                        f"\u203a task\n\u2022 {response}\n\n\u203a \n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_schema_content"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        expected_schema = {
            "type": "object",
            "required": ["verdict"],
            "properties": {"verdict": {"type": "string", "enum": ["approved", "changes_requested"]}},
        }
        contract = {"format": "json", "json_schema": expected_schema}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
            session_mode="ephemeral",
        )
        host = TmuxSchemaCapture()
        session = host.get_or_create_session(agent_id="agt_schema_content")

        claimed = {
            "agent_id": "agt_schema_content",
            "job": {"job_id": "job_sc", "output_contract_json": contract},
            "run": {"run_id": "run_schema_content_test"},
            "message": {"text": "review"},
        }

        adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())

        # (2) Schema file contents must match the json_schema from the contract
        self.assertEqual(len(captured_schema_content), 1, "expected exactly one schema file write")
        written = json.loads(captured_schema_content[0])
        self.assertEqual(written, expected_schema)

    def test_codex_tui_heartbeat_filters_noise_from_last_line(self) -> None:
        """Codex heartbeat last_line should skip noise lines like status bar text."""

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_noise"})()})()
                self.progress: list[dict] = []

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                self.progress.append({"message": message, "details": details or {}})
                return {"status": "ok"}

        adapter = CodexAdapter(tui_mode=True, cli_command="codex")
        session = type("S", (), {"session_id": "s1", "metadata": {}, "workspace_ref": None})()
        sup = SupervisorStub()
        claimed = {
            "agent_id": "agt_noise",
            "job": {"job_id": "job_noise"},
            "run": {"run_id": "run_noise"},
            "message": {"text": "explain code"},
        }

        # Token usage line should be filtered, returning the real content
        adapter._emit_progress_heartbeat(
            supervisor=sup,
            claimed=claimed,
            session=session,
            stage="tui",
            changed=True,
            poll=1,
            output_chars=100,
            output_delta="real content\nToken usage: 500 \u00b7 context left 80%\n",
            tui_state="working",
        )
        hb = sup.progress[0]["details"]
        self.assertEqual(hb["last_line"], "real content")
        self.assertEqual(hb["tui_state"], "working")

        # Working status line should also be filtered
        sup.progress.clear()
        adapter._emit_progress_heartbeat(
            supervisor=sup,
            claimed=claimed,
            session=session,
            stage="tui",
            changed=True,
            poll=2,
            output_chars=200,
            output_delta="thinking about the problem\nWorking (5s \u00b7 esc to interrupt)\n",
            tui_state="working",
        )
        self.assertEqual(sup.progress[0]["details"]["last_line"], "thinking about the problem")

    def test_codex_tui_heartbeat_skips_bare_prompt_marker(self) -> None:
        """Heartbeat last_line should not be a bare \u203a prompt marker."""

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_prompt"})()})()
                self.progress: list[dict] = []

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                self.progress.append({"message": message, "details": details or {}})
                return {"status": "ok"}

        adapter = CodexAdapter(tui_mode=True, cli_command="codex")
        session = type("S", (), {"session_id": "s1", "metadata": {}, "workspace_ref": None})()
        sup = SupervisorStub()
        claimed = {
            "agent_id": "agt_p",
            "job": {"job_id": "job_p"},
            "run": {"run_id": "run_p"},
            "message": {"text": "test"},
        }
        adapter._emit_progress_heartbeat(
            supervisor=sup,
            claimed=claimed,
            session=session,
            stage="tui",
            changed=True,
            poll=1,
            output_chars=10,
            output_delta="actual content\n\u203a \n",
            tui_state="ready",
        )
        self.assertEqual(sup.progress[0]["details"]["last_line"], "actual content")

    def test_codex_tui_heartbeat_emits_tui_state(self) -> None:
        """Codex TUI heartbeats should include tui_state for CLI consumption."""

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_state"})()})()
                self.progress: list[dict] = []

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                self.progress.append({"message": message, "details": details or {}})
                return {"status": "ok"}

        adapter = CodexAdapter(tui_mode=True, cli_command="codex")
        session = type("S", (), {"session_id": "s1", "metadata": {}, "workspace_ref": None})()
        sup = SupervisorStub()
        claimed = {
            "agent_id": "agt_s",
            "job": {"job_id": "job_s"},
            "run": {"run_id": "run_s"},
            "message": {"text": "test"},
        }
        for state in ("working", "completed", "ready", "gate.auto", ""):
            sup.progress.clear()
            adapter._emit_progress_heartbeat(
                supervisor=sup,
                claimed=claimed,
                session=session,
                stage="tui",
                changed=True,
                poll=1,
                output_chars=10,
                output_delta="line\n",
                tui_state=state,
            )
            self.assertEqual(sup.progress[0]["details"]["tui_state"], state)

    def test_extract_exec_response_finds_codex_marker_json(self) -> None:
        """_extract_exec_response should extract JSON after the 'codex' marker line."""
        from agp.plugins.codex import _extract_exec_response
        exec_output = (
            "OpenAI Codex v0.117.0\n"
            "--------\n"
            "workdir: /Users/pb/projects/skynet\n"
            "model: gpt-5.4\n"
            "--------\n"
            "user\n"
            "Read the task file and follow it.\n"
            "exec\n"
            "/bin/zsh -lc \"git diff HEAD~1\"\n"
            " succeeded in 0ms:\n"
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,5 +1,7 @@\n"
            " import json\n"
            "+import logging\n"
            " def foo():\n"
            '     return {"key": "value"}\n'
            "+    _logger = logging.getLogger(__name__)\n"
            "codex\n"
            '{"verdict":"approved","summary":"looks good","findings":[]}\n'
            "tokens used\n"
            "14,309\n"
            '{"verdict":"approved","summary":"looks good","findings":[]}\n'
        )
        result = _extract_exec_response(exec_output)
        self.assertEqual(
            json.loads(result),
            {"verdict": "approved", "summary": "looks good", "findings": []},
        )

    def test_extract_exec_response_skips_tool_output_with_braces(self) -> None:
        """Tool output containing { and [ should not be returned as the response."""
        from agp.plugins.codex import _extract_exec_response
        # Simulate exec output where tool output is hundreds of lines of Python
        tool_output_lines = []
        for i in range(200):
            tool_output_lines.append(f'    if data[{i}] == {{"key": {i}}}:')
        tool_output = "\n".join(tool_output_lines)
        exec_output = (
            "user\n"
            "Review the diff\n"
            "exec\n"
            "/bin/zsh -lc \"git diff\"\n"
            " succeeded in 0ms:\n"
            f"{tool_output}\n"
            "codex\n"
            '{"verdict":"changes_requested","summary":"found issues","findings":[]}\n'
            "tokens used\n"
            "60,518\n"
            '{"verdict":"changes_requested","summary":"found issues","findings":[]}\n'
        )
        result = _extract_exec_response(exec_output)
        parsed = json.loads(result)
        self.assertEqual(parsed["verdict"], "changes_requested")

    def test_extract_exec_response_no_codex_marker_finds_last_json_line(self) -> None:
        """When codex marker scrolled off, find JSON as last non-prompt line."""
        from agp.plugins.codex import _extract_exec_response
        # Simulate scrollback where 'codex' marker is missing
        exec_output = (
            "exec\n"
            '/bin/zsh -lc "git diff"\n'
            " succeeded in 0ms:\n"
            "diff --git a/foo.py b/foo.py\n"
            '+import logging\n'
            '{"verdict":"approved","summary":"ok","findings":[]}\n'
            "tokens used\n"
            "14,309\n"
            '{"verdict":"approved","summary":"ok","findings":[]}\n'
            "\n"
            "~/projects/skynet spin at 18:26:52\n"
            "\u276f \n"
        )
        result = _extract_exec_response(exec_output)
        parsed = json.loads(result)
        self.assertEqual(parsed["verdict"], "approved")

    def test_codex_exec_mode_extracts_json_not_tool_traces(self) -> None:
        """Exec mode result extraction should find JSON response, not tool trace content."""

        class TmuxExecHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._poll_count = 0

            def reset_session(self, session):
                return super().reset_session(session)

            def launch_command(self, session, *, command, env=None, cwd=None):
                # Simulate exec mode output: tool traces with code, then JSON
                tool_output = "\n".join(
                    f'    if data[{i}] == {{"key": {i}}}:'
                    for i in range(100)
                )
                exec_output = (
                    "OpenAI Codex v0.117.0\n"
                    "--------\n"
                    "user\n"
                    "Review the diff\n"
                    "exec\n"
                    '/bin/zsh -lc "git diff"\n'
                    " succeeded in 0ms:\n"
                    f"{tool_output}\n"
                    "codex\n"
                    '{"verdict":"approved","summary":"all good","findings":[]}\n'
                    "tokens used\n"
                    "14,309\n"
                    '{"verdict":"approved","summary":"all good","findings":[]}\n'
                )
                self._history.setdefault(session.session_id, []).append(exec_output)
                # Simulate the stdout redirect: write the JSON to the stdout file
                # that the adapter created (extract from the > redirect in the command).
                if ">" in command:
                    stdout_path = command.split(">")[-1].strip().strip("'\"")
                    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(stdout_path).write_text(
                        '{"verdict":"approved","summary":"all good","findings":[]}\n',
                        encoding="utf-8",
                    )
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def read_visible(self, session):
                # After a few polls, simulate shell return (exec exits)
                self._poll_count += 1
                if self._poll_count >= 2:
                    return "$ "
                return super().read_visible(session)

            def _get_pane_tty(self, session):
                return None

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_exec_json"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        expected_schema = {
            "type": "object",
            "required": ["verdict", "summary", "findings"],
            "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["approved", "changes_requested"]},
                "summary": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "object",
                    "required": ["severity", "description", "file", "line"],
                    "additionalProperties": False,
                    "properties": {
                        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                        "description": {"type": "string"},
                        "file": {"type": ["string", "null"]},
                        "line": {"type": ["integer", "null"]},
                    }}},
            },
        }
        contract = {"format": "json", "json_schema": expected_schema}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
            session_mode="ephemeral",
        )
        host = TmuxExecHost()
        session = host.get_or_create_session(agent_id="agt_exec_json")

        claimed = {
            "agent_id": "agt_exec_json",
            "job": {"job_id": "job_ej", "output_contract_json": contract},
            "run": {"run_id": "run_exec_json_test"},
            "message": {"text": "review"},
        }

        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        result_content = result.artifacts[-1].content
        parsed = json.loads(result_content)
        self.assertEqual(parsed["verdict"], "approved")
        self.assertEqual(parsed["summary"], "all good")

    def test_codex_looks_like_working_not_blocked_by_noise_filter(self) -> None:
        """_looks_like_working must detect 'Working (' even though it's in _NOISE_PREFIXES."""
        adapter = CodexAdapter(tui_mode=True, cli_command="codex")
        # Screen where "Working (" is the last meaningful line
        screen = (
            "\u203a Review the diff\n"
            "\n"
            "Working (3s \u00b7 esc to interrupt)\n"
        )
        self.assertTrue(adapter._looks_like_working(screen))

    def test_codex_looks_like_working_with_noise_around(self) -> None:
        """Working indicator should be found even when surrounded by noise lines."""
        adapter = CodexAdapter(tui_mode=True, cli_command="codex")
        screen = (
            "\u203a Do something\n"
            "\n"
            "Working (12s \u00b7 esc to interrupt)\n"
            "Token usage: 1,234 tokens\n"
            "\n"
        )
        self.assertTrue(adapter._looks_like_working(screen))

    def test_codex_tui_state_reports_working(self) -> None:
        """tui_state should report 'working' when the Working indicator is visible."""
        adapter = CodexAdapter(tui_mode=True, cli_command="codex")
        screen = (
            "\u203a Review the diff\n"
            "\n"
            "Working (3s \u00b7 esc to interrupt)\n"
        )
        # _looks_like_working should return True
        self.assertTrue(adapter._looks_like_working(screen))
        # And it should NOT match completed or ready (which would preempt working in the tui_state cascade)
        self.assertFalse(adapter._looks_like_completed_turn(
            screen, baseline_answered_turns=0, baseline_last_response=None,
        ))

    def test_codex_exec_mode_raises_on_empty_stdout_after_shell_return(self) -> None:
        """Exec mode should raise AdapterExecutionFailed when stdout stays empty after shell returns."""
        from agp.runtime import AdapterExecutionFailed

        class TmuxExecFailHost(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self._poll_count = 0

            def reset_session(self, session):
                return super().reset_session(session)

            def launch_command(self, session, *, command, env=None, cwd=None):
                self._history.setdefault(session.session_id, []).append("")
                # Create the stdout file but leave it EMPTY (simulating exec failure)
                if ">" in command:
                    stdout_path = command.split(">")[-1].strip().strip("'\"")
                    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(stdout_path).write_text("", encoding="utf-8")
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def read_visible(self, session):
                self._poll_count += 1
                if self._poll_count >= 2:
                    return "$ "  # shell returned
                return super().read_visible(session)

            def _get_pane_tty(self, session):
                return None

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_fail"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        expected_schema = {
            "type": "object",
            "required": ["result"],
            "properties": {"result": {"type": "string"}},
        }
        contract = {"format": "json", "json_schema": expected_schema}

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="codex",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=2.0,
            session_mode="ephemeral",
        )
        host = TmuxExecFailHost()
        session = host.get_or_create_session(agent_id="agt_exec_fail")

        claimed = {
            "agent_id": "agt_exec_fail",
            "job": {"job_id": "job_ef", "output_contract_json": contract},
            "run": {"run_id": "run_exec_fail_test"},
            "message": {"text": "review"},
        }

        with self.assertRaises(AdapterExecutionFailed) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("empty", str(ctx.exception).lower())

    def test_codex_ephemeral_tmux_contract_wrapper_command_inserts_exec_correctly(self) -> None:
        """For wrapper commands like 'python -m codex --full-auto', exec must be
        inserted after the codex token, not after position 0."""

        class TmuxCapture(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self.launched_commands: list[str] = []

            def reset_session(self, session):
                return super().reset_session(session)

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.launched_commands.append(command)
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "codex" in text:
                    response = json.dumps({"verdict": "approved", "summary": "ok"})
                    self._history.setdefault(session.session_id, []).append(
                        f"\u203a task\n\u2022 {response}\n\n\u203a \n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_wrap"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        contract = {
            "format": "json",
            "json_schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
        }

        # Test wrapper: "python -m codex --full-auto"
        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="python -m codex --full-auto",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
            session_mode="ephemeral",
        )
        host = TmuxCapture()
        session = host.get_or_create_session(agent_id="agt_wrap")
        claimed = {
            "agent_id": "agt_wrap",
            "job": {"job_id": "job_wrap", "output_contract_json": contract},
            "run": {"run_id": "run_wrapper_test"},
            "message": {"text": "review"},
        }

        adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())

        self.assertTrue(host.launched_commands)
        cmd = host.launched_commands[-1]
        # "codex exec" must appear as a unit, after "python -m"
        self.assertIn("python -m codex exec", cmd,
                       f"'exec' should follow 'codex' in wrapper command: {cmd}")
        self.assertIn("--output-schema", cmd)

    def test_codex_ephemeral_tmux_contract_shell_wrapper_inserts_exec_inside(self) -> None:
        """For shell wrappers like bash -lc 'codex --full-auto', exec and extra
        args must be inserted inside the inner command quotes."""

        class TmuxCapture(InProcessTerminalHost):
            @property
            def kind(self) -> str:
                return "tmux"

            def __init__(self) -> None:
                super().__init__()
                self.launched_commands: list[str] = []

            def reset_session(self, session):
                return super().reset_session(session)

            def launch_command(self, session, *, command, env=None, cwd=None):
                self.launched_commands.append(command)
                self.send_text(session, command, enter=True)
                return subprocess.Popen(["true"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if "codex" in text:
                    response = json.dumps({"verdict": "approved", "summary": "ok"})
                    self._history.setdefault(session.session_id, []).append(
                        f"\u203a task\n\u2022 {response}\n\n\u203a \n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_sh"})()})()

            def check_interrupt(self, claimed):
                return None

            def emit_progress(self, claimed, *, message, details=None):
                return {"status": "ok"}

        contract = {
            "format": "json",
            "json_schema": {"type": "object", "properties": {"verdict": {"type": "string"}}},
        }

        adapter = CodexAdapter(
            tui_mode=True,
            cli_command="bash -lc 'codex --full-auto'",
            idle_poll_seconds=0.0,
            idle_after=2,
            idle_timeout_seconds=1.0,
            session_mode="ephemeral",
        )
        host = TmuxCapture()
        session = host.get_or_create_session(agent_id="agt_sh")
        claimed = {
            "agent_id": "agt_sh",
            "job": {"job_id": "job_sh", "output_contract_json": contract},
            "run": {"run_id": "run_shell_wrapper_test"},
            "message": {"text": "review"},
        }

        adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())

        self.assertTrue(host.launched_commands)
        cmd = host.launched_commands[-1]
        # "exec" and "--output-schema" must be inside the quotes
        self.assertIn("codex exec", cmd,
                       f"'exec' should follow 'codex': {cmd}")
        self.assertIn("--output-schema", cmd)
        # The closing quote must come AFTER the extra args, not before
        self.assertTrue(cmd.rstrip().endswith("'"),
                        f"shell wrapper closing quote should be at the end: {cmd}")
        # --output-schema must appear before the closing quote
        last_quote_idx = cmd.rstrip().rfind("'")
        schema_idx = cmd.find("--output-schema")
        self.assertLess(schema_idx, last_quote_idx,
                        f"--output-schema must be inside wrapper quotes: {cmd}")
