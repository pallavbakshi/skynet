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

    def test_strip_ansi_removes_escape_sequences(self) -> None:
        raw = "\x1b[32mgreen\x1b[0m plain \x1b[1;31mbold-red\x1b[0m"
        self.assertEqual(_strip_ansi(raw), "green plain bold-red")

    def test_strip_ansi_handles_osc_sequences(self) -> None:
        raw = "\x1b]0;title\x07visible"
        self.assertEqual(_strip_ansi(raw), "visible")

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

    def test_build_agent_adapter_claude_code(self) -> None:
        """build_agent_adapter should return ClaudeCodeAdapter for kind='claude_code'."""
        adapter = build_agent_adapter("claude_code")
        self.assertEqual(adapter.kind, "claude_code")
        self.assertIsInstance(adapter, ClaudeCodeAdapter)

