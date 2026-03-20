"""Regression tests for the AGP MVP control loop."""

from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path
from threading import Thread
from time import sleep
from tempfile import mkdtemp
import shutil
import typer
import json
import httpx

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from agp.config import settings
import agp.control_plane as control_plane_module
from agp.artifact_store import reset_artifact_store_state
from agp.control_plane import build_app
from agp.db import Base, SessionLocal, engine, init_db
from agp.models import Agent, Capability, Event, QueueDeliveryRecord, Runtime, utc_now
from agp.enums import HealthStatus, RuntimeStatus
from agp.cli import (
    fetch_artifact_via_api,
    interrupt_job_via_api,
    list_agents_via_api,
    list_job_artifacts_via_api,
    list_jobs_via_api,
    list_queue_deliveries_via_api,
    list_run_artifacts_via_api,
    observability_alerts_via_api,
    observability_control_plane_logs_via_api,
    observability_job_trace_via_api,
    observability_runtime_logs_via_api,
    observability_summary_via_api,
    rotate_operator_tokens_via_api,
    rotate_runtime_tokens_via_api,
    send_message_via_api,
    system_auth_status_via_api,
    watch_job_until_terminal,
    create_backup_snapshot,
    get_upgrade_status,
    mark_upgrade,
    prune_observability_logs,
    reconstruct_queue_from_state,
    restore_and_recover_snapshot,
    rollback_to_previous_version,
    run_failure_injection_scenario,
    restore_backup_snapshot,
    validate_restored_state,
)
from agp.runtime import (
    CodexAdapter,
    DefaultAgentAdapter,
    InProcessTerminalHost,
    OutputCursor,
    RecoverableExecutionError,
    RuntimeClient,
    RuntimeIdentity,
    RuntimeSupervisor,
    WezTermHost,
    _OutputAccumulator,
    _clean_codex_tui_output,
    _compute_output_delta,
    _strip_ansi,
    build_agent_adapter,
    build_terminal_host,
)
from agp.control_plane import (
    sweep_draining_agents,
    sweep_draining_runtimes,
    sweep_expired_leases,
    sweep_idle_agents,
    sweep_stale_runtimes,
)
from agp.control_plane import _block_job, _require_job, _unblock_job
from agp.queue_backend import get_queue_backend, reset_queue_backend_state
import agp.queue_backend as queue_backend_module
from agp.sweeper import SweeperService


class FakeRedisClient:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}

    def flushdb(self) -> None:
        self.lists.clear()
        self.hashes.clear()
        self.sets.clear()

    def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    def lpop(self, key: str) -> str | None:
        values = self.lists.setdefault(key, [])
        if not values:
            return None
        return values.pop(0)

    def hset(self, name: str, key: str, value: str) -> None:
        self.hashes.setdefault(name, {})[key] = value

    def hget(self, name: str, key: str) -> str | None:
        return self.hashes.get(name, {}).get(key)

    def hdel(self, name: str, key: str) -> None:
        self.hashes.setdefault(name, {}).pop(key, None)

    def hkeys(self, name: str) -> list[str]:
        return list(self.hashes.get(name, {}).keys())

    def sadd(self, name: str, value: str) -> None:
        self.sets.setdefault(name, set()).add(value)

    def srem(self, name: str, value: str) -> None:
        self.sets.setdefault(name, set()).discard(value)

    def sismember(self, name: str, value: str) -> bool:
        return value in self.sets.setdefault(name, set())


class MvpFlowTest(unittest.TestCase):
    def _materialize_terminal_artifacts(self, names_to_roles: dict[str, str]) -> list[dict]:
        base = Path(mkdtemp(prefix="agp-test-artifacts-"))
        refs: list[dict] = []
        for name, role in names_to_roles.items():
            path = base / name
            content = f"{role}:{name}\n"
            path.write_text(content, encoding="utf-8")
            refs.append(
                {
                    "role": role,
                    "storage_ref": path.resolve().as_uri(),
                    "content_type": "text/plain",
                    "checksum": "",
                    "size_bytes": path.stat().st_size,
                }
            )
        return refs

    def setUp(self) -> None:
        settings.operator_bearer_token = None
        settings.operator_token_roles_json = {}
        settings.runtime_active_tokens_json = []
        settings.runtime_bearer_token = None
        settings.queue_backend = "delivery_table"
        settings.redis_url = "redis://test"
        settings.redis_queue_key_prefix = "agp-test"
        settings.artifact_backend = "localfs"
        settings.observability_unreachable_runtime_threshold = 1
        settings.observability_expired_lease_alert_threshold = 3
        settings.observability_dead_letter_alert_threshold = 1
        settings.observability_terminal_failure_sample_size = 3
        settings.observability_terminal_failure_rate_threshold = 0.5
        queue_backend_module._REDIS_CLIENT_FACTORY = None
        reset_queue_backend_state()
        reset_artifact_store_state()
        control_plane_module._event_seq_counter = None
        if settings.log_root.exists():
            shutil.rmtree(settings.log_root)
        engine.dispose()
        Base.metadata.drop_all(bind=engine)
        init_db()
        session = SessionLocal()
        try:
            session.add(
                Capability(
                    capability_id="cap_python",
                    name="Python Tester",
                    version="v1",
                    image_ref="python:3.12",
                    model_ref="gpt-5.4",
                    resource_tier="default",
                    permission_profile="default",
                    queue_mode="agent",
                    runtime_requirements_json={},
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            session.commit()
        finally:
            session.close()
        if settings.artifact_root.exists():
            shutil.rmtree(settings.artifact_root)
        settings.artifact_root.mkdir(parents=True, exist_ok=True)
        self.client = TestClient(build_app())

    def tearDown(self) -> None:
        self.client.close()
        Base.metadata.drop_all(bind=engine)

    def test_agent_targeted_job_completes(self) -> None:
        agent = self.client.post("/agents/up", json={"agent_id": "agt_one", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)

        runtime = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_one", "hostname": "localhost"},
        )
        self.assertEqual(runtime.status_code, 200)

        send = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_one"},
                "message": {"text": "hello", "metadata": {}},
            },
            headers={"Idempotency-Key": "mvp-flow-1"},
        )
        self.assertEqual(send.status_code, 200)
        job_id = send.json()["data"]["job_id"]

        replay = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_one"},
                "message": {"text": "hello", "metadata": {}},
            },
            headers={"Idempotency-Key": "mvp-flow-1"},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["data"]["job_id"], job_id)

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_one", "agent_id": "agt_one"},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]
        self.assertTrue(claim_data["claimed"])

        run_id = claim_data["run"]["run_id"]
        lease_id = claim_data["lease"]["lease_id"]
        fencing_token = claim_data["lease"]["fencing_token"]

        heartbeat = self.client.post(
            f"/runs/{run_id}/heartbeat",
            json={
                "runtime_id": "rtm_one",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
            },
        )
        self.assertEqual(heartbeat.status_code, 200)

        progress = self.client.post(
            f"/runs/{run_id}/progress",
            json={
                "runtime_id": "rtm_one",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "message": "step-1",
                "details": {"phase": "working"},
            },
        )
        self.assertEqual(progress.status_code, 200)

        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_one",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "artifacts": self._materialize_terminal_artifacts(
                    {
                        "prompt.txt": "prompt",
                        "transcript.txt": "transcript_log",
                        "exec.txt": "exec_log",
                        "result.txt": "result",
                    }
                ),
                "summary": {"ok": True},
            },
        )
        self.assertEqual(complete.status_code, 200)

        job = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["data"]["status"], "completed")
        self.assertIsNotNone(job.json()["data"]["result_artifact_id"])

        events = self.client.get(f"/jobs/{job_id}/events")
        self.assertEqual(events.status_code, 200)
        event_types = [item["event_type"] for item in events.json()["data"]["items"]]
        self.assertIn("job.accepted", event_types)
        self.assertIn("job.queued", event_types)
        self.assertIn("run.created", event_types)
        self.assertIn("lease.acquired", event_types)
        self.assertIn("run.running", event_types)
        self.assertIn("lease.heartbeat", event_types)
        self.assertIn("run.progress", event_types)
        self.assertIn("artifact.created", event_types)
        self.assertIn("run.completed", event_types)
        self.assertIn("job.completed", event_types)

    def test_inline_send_returns_inline_result_for_idle_agent(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_inline", "capability_id": "cap_python"})
        response = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_inline"},
                "message": {"text": "inline please", "metadata": {}},
                "detach_policy": {"mode": "inline"},
            },
            headers={"Idempotency-Key": "inline-flow-1"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["kind"], "inline_result")
        self.assertEqual(payload["status"], "completed")
        job = self.client.get(f"/jobs/{payload['job_id']}").json()["data"]
        self.assertEqual(job["status"], "completed")
        self.assertIsNotNone(job["result_artifact_id"])

    def test_jobs_list_uses_cursor_pagination(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_page", "capability_id": "cap_python"})
        for idx in range(3):
            self.client.post(
                "/messages/send",
                json={
                    "target": {"type": "agent", "id": "agt_page"},
                    "message": {"text": f"page-{idx}", "metadata": {}},
                },
                headers={"Idempotency-Key": f"page-flow-{idx}"},
            )
        first = self.client.get("/jobs", params={"limit": 2})
        self.assertEqual(first.status_code, 200)
        first_payload = first.json()["data"]
        self.assertEqual(len(first_payload["items"]), 2)
        self.assertTrue(first_payload["page"]["has_more"])
        cursor = first_payload["page"]["next_cursor"]
        self.assertIsNotNone(cursor)
        second = self.client.get("/jobs", params={"limit": 2, "cursor": cursor})
        self.assertEqual(second.status_code, 200)
        second_payload = second.json()["data"]
        self.assertGreaterEqual(len(second_payload["items"]), 1)
        first_ids = {item["job_id"] for item in first_payload["items"]}
        second_ids = {item["job_id"] for item in second_payload["items"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))

    def test_agents_and_runtimes_support_basic_filters(self) -> None:
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_filter", "hostname": "localhost"},
        )
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_filter", "capability_id": "cap_python", "assigned_runtime_id": "rtm_filter"},
        )
        agents = self.client.get("/agents", params={"capability_id": "cap_python", "status": "idle"})
        self.assertEqual(agents.status_code, 200)
        self.assertEqual(len(agents.json()["data"]["items"]), 1)

        runtimes = self.client.get("/runtimes", params={"status": "idle", "health_status": "healthy"})
        self.assertEqual(runtimes.status_code, 200)
        self.assertEqual(len(runtimes.json()["data"]["items"]), 1)

    def test_runtime_worker_can_claim_and_complete(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_runtime", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_runtime"},
                "message": {"text": "run via worker", "metadata": {"kind": "worker"}},
            },
            headers={"Idempotency-Key": "worker-flow-1"},
        )

        runtime_client = RuntimeClient(
            RuntimeIdentity(runtime_id="rtm_worker", hostname="localhost", server_url="http://testserver"),
            client=self.client,
        )
        worker = RuntimeSupervisor(
            runtime_client,
            host=InProcessTerminalHost(),
            adapter=DefaultAgentAdapter(),
            artifact_root=".agp-artifacts-tests",
        )
        try:
            payload = worker.run_once(agent_id="agt_runtime")
        finally:
            runtime_client.close()

        self.assertTrue(payload["claimed"])
        self.assertEqual(payload["result"]["status"], "completed")
        job_id = payload["claim"]["job"]["job_id"]
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "completed")
        self.assertIsNotNone(job["result_artifact_id"])

    def test_build_runtime_plugin_factories_support_inprocess_and_wezterm(self) -> None:
        self.assertEqual(build_terminal_host("inprocess").kind, "inprocess")
        self.assertEqual(build_terminal_host("wezterm", runner=lambda *args, **kwargs: None).kind, "wezterm")
        self.assertEqual(build_agent_adapter("default").kind, "default")

    def test_wezterm_host_maps_terminal_operations_to_cli_calls(self) -> None:
        calls: list[tuple[list[str], str | None]] = []

        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        state = {"listed": 0}

        def runner(argv: list[str], input: str | None = None, **_: object) -> Result:
            calls.append((argv, input))
            command = argv[2]
            if command == "list":
                state["listed"] += 1
                if state["listed"] == 1:
                    return Result("[]")
                return Result(
                    json.dumps(
                        [
                            {
                                "pane_id": 4242,
                                "tab_id": 7,
                                "window_id": 9,
                                "workspace": "agp-test",
                                "window_title": "AGP:agt_wez",
                                "tab_title": "AGP:agt_wez",
                                "cwd": "file:///tmp/agt_wez",
                            }
                        ]
                    )
                )
            if command == "spawn":
                return Result("4242\n")
            if command in {"set-window-title", "set-tab-title", "kill-pane", "send-text"}:
                return Result("")
            if command == "get-text":
                return Result("session output\n")
            raise AssertionError(f"unexpected wezterm command: {argv}")

        host = WezTermHost(workspace="agp-test", shell_argv=["zsh", "-l"], runner=runner)
        session = host.get_or_create_session(agent_id="agt_wez", workspace_ref="/tmp/agt_wez")
        self.assertEqual(session.session_id, "4242")
        self.assertTrue(host.session_exists(session))
        health = host.health(session)
        self.assertTrue(health.exists)
        self.assertTrue(health.healthy)
        self.assertEqual(health.metadata["pane_id"], 4242)
        host.send_text(session, "hello", enter=True)
        host.interrupt(session)
        snapshot = host.snapshot(session)
        host.terminate_session(session)

        self.assertEqual(snapshot["session_id"], "4242")
        self.assertEqual(snapshot["text"], "session output\n")
        command_names = [argv[2] for argv, _ in calls]
        self.assertIn("spawn", command_names)
        self.assertIn("set-window-title", command_names)
        self.assertIn("set-tab-title", command_names)
        self.assertIn("get-text", command_names)
        self.assertIn("kill-pane", command_names)
        send_calls = [argv for argv, _ in calls if argv[2] == "send-text"]
        self.assertGreaterEqual(len(send_calls), 3)

    def test_inprocess_terminal_host_cursor_reads_incremental_output(self) -> None:
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_cursor")
        self.assertTrue(host.session_exists(session))
        self.assertTrue(host.health(session).healthy)
        cursor = host.create_cursor(session)
        host.send_text(session, "first", enter=True)
        first = host.read_output(session, cursor)
        self.assertTrue(first.changed)
        self.assertIn("SEND:first", first.text)
        second = host.read_output(session, first.cursor)
        self.assertFalse(second.changed)
        self.assertEqual(second.text, "")
        host.send_text(session, "second", enter=False)
        third = host.read_output(session, second.cursor)
        self.assertTrue(third.changed)
        self.assertIn("SEND:second", third.text)
        host.terminate_session(session)
        self.assertFalse(host.session_exists(session))
        self.assertEqual(host.health(session).reason, "session_missing")

    def test_wezterm_host_cursor_reads_incremental_output(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        get_text_responses = iter(["baseline\n", "baseline\nnew line\n", "baseline\nnew line\n"])

        def runner(argv: list[str], input: str | None = None, **_: object) -> Result:  # noqa: ARG001
            if argv[2] == "get-text":
                return Result(next(get_text_responses))
            if argv[2] == "list":
                return Result(
                    json.dumps(
                        [
                            {
                                "pane_id": 55,
                                "tab_id": 7,
                                "window_id": 9,
                                "workspace": "agp-test",
                                "window_title": "AGP:agt_wez_cursor",
                                "tab_title": "AGP:agt_wez_cursor",
                                "cwd": "file:///tmp/agt_wez_cursor",
                            }
                        ]
                    )
                )
            raise AssertionError(f"unexpected wezterm command: {argv}")

        host = WezTermHost(workspace="agp-test", runner=runner)
        session = host.get_or_create_session(agent_id="agt_wez_cursor", workspace_ref="/tmp/agt_wez_cursor")
        cursor = host.create_cursor(session)
        first = host.read_output(session, cursor)
        self.assertTrue(first.changed)
        self.assertEqual(first.text, "new line\n")
        second = host.read_output(session, first.cursor)
        self.assertFalse(second.changed)
        self.assertEqual(second.text, "")

    def test_codex_adapter_completes_on_marker_output(self) -> None:
        class CodexHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("AGP_RUN_BEGIN run_codex"):
                    self._history.setdefault(session.session_id, []).append(
                        'AGP_RUN_RESULT run_codex {"status":"success","result":"success payload"}\n'
                    )

        host = CodexHost()
        session = host.get_or_create_session(agent_id="agt_codex")
        host.send_text(session, "bootstrap noise", enter=True)
        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_codex"})()})()
                self.progress: list[dict] = []

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                self.progress.append({"message": message, "details": details or {}})
                return {"status": "ok"}

        supervisor = SupervisorStub()
        claimed = {
            "agent_id": "agt_codex",
            "job": {"job_id": "job_codex"},
            "run": {"run_id": "run_codex"},
            "message": {"text": "do work"},
        }
        adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=supervisor)
        roles = [artifact.role for artifact in result.artifacts]
        self.assertEqual(roles, ["prompt", "transcript_log", "exec_log", "result"])
        self.assertEqual(result.artifacts[-1].content, "success payload")

    def test_codex_adapter_ignores_terminal_lines_for_other_run_ids(self) -> None:
        class CodexHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("AGP_RUN_BEGIN run_live"):
                    self._history.setdefault(session.session_id, []).append(
                        'AGP_RUN_RESULT run_old {"status":"success","result":"stale payload"}\n'
                    )
                    self._history.setdefault(session.session_id, []).append(
                        'AGP_RUN_RESULT run_live {"status":"success","result":"fresh payload"}\n'
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_codex"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)
        host = CodexHost()
        session = host.get_or_create_session(agent_id="agt_codex_scoped")
        claimed = {
            "agent_id": "agt_codex_scoped",
            "job": {"job_id": "job_codex_scoped"},
            "run": {"run_id": "run_live"},
            "message": {"text": "do scoped work"},
        }
        result = adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertEqual(result.artifacts[-1].content, "fresh payload")

    def test_codex_adapter_failure_payload_becomes_failure_artifacts(self) -> None:
        class CodexHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("AGP_RUN_BEGIN run_codex_fail"):
                    self._history.setdefault(session.session_id, []).append(
                        'AGP_RUN_RESULT run_codex_fail {"status":"failure","error":"tool chain broke"}\n'
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_codex"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)
        host = CodexHost()
        session = host.get_or_create_session(agent_id="agt_codex_fail")
        claimed = {
            "agent_id": "agt_codex_fail",
            "job": {"job_id": "job_codex_fail"},
            "run": {"run_id": "run_codex_fail"},
            "message": {"text": "fail work"},
        }
        supervisor = SupervisorStub()
        with self.assertRaises(Exception) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=supervisor)
        failure_result = adapter.build_failure_result(
            host=host,
            session=session,
            claimed=claimed,
            error=ctx.exception,
            supervisor=supervisor,
        )
        roles = [artifact.role for artifact in failure_result.artifacts]
        self.assertIn("transcript_log", roles)
        self.assertIn("exec_log", roles)
        self.assertIn("failure_evidence", roles)
        self.assertTrue(any("tool chain broke" in artifact.content for artifact in failure_result.artifacts))

    def test_runtime_worker_completes_with_registryfs_artifacts(self) -> None:
        settings.artifact_backend = "registryfs"
        self.client.post("/agents/up", json={"agent_id": "agt_runtime_registry", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_runtime_registry"},
                "message": {"text": "run via worker registry", "metadata": {"kind": "worker"}},
            },
            headers={"Idempotency-Key": "worker-registry-flow-1"},
        )

        runtime_client = RuntimeClient(
            RuntimeIdentity(runtime_id="rtm_worker_registry", hostname="localhost", server_url="http://testserver"),
            client=self.client,
        )
        worker = RuntimeSupervisor(
            runtime_client,
            host=InProcessTerminalHost(),
            adapter=DefaultAgentAdapter(),
            artifact_root=settings.artifact_root,
        )
        try:
            payload = worker.run_once(agent_id="agt_runtime_registry")
        finally:
            runtime_client.close()

        self.assertTrue(payload["claimed"])
        self.assertEqual(payload["result"]["status"], "completed")
        job_id = payload["claim"]["job"]["job_id"]
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "completed")
        artifact = self.client.get(f"/artifacts/{job['result_artifact_id']}").json()["data"]
        self.assertTrue(artifact["storage_ref"].startswith("agpr://"))
        self.assertNotEqual(artifact["checksum"], "")

    def test_job_can_transition_blocked_to_queued_via_operator_helpers(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_block", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_block"},
                "message": {"text": "block me", "metadata": {}},
            },
            headers={"Idempotency-Key": "block-flow-1"},
        ).json()
        job_id = sent["data"]["job_id"]
        session = SessionLocal()
        try:
            job = _require_job(session, job_id)
            _block_job(session, job=job, reason="waiting_on_dependency")
            session.commit()
            self.assertEqual(job.status, "blocked")
            _unblock_job(session, job=job, reason="dependency_resolved")
            session.commit()
            self.assertEqual(job.status, "queued")
        finally:
            session.close()

        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "queued")

    def test_job_can_transition_blocked_to_queued_via_http_endpoints(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_block_http", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_block_http"},
                "message": {"text": "http block me", "metadata": {}},
            },
            headers={"Idempotency-Key": "block-http-flow-1"},
        ).json()
        job_id = sent["data"]["job_id"]

        blocked = self.client.post(f"/jobs/{job_id}/block", params={"reason": "waiting_on_dependency"})
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked.json()["data"]["status"], "blocked")

        unblocked = self.client.post(f"/jobs/{job_id}/unblock", params={"reason": "dependency_resolved"})
        self.assertEqual(unblocked.status_code, 200)
        self.assertEqual(unblocked.json()["data"]["status"], "queued")

        events = self.client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
        event_types = [item["event_type"] for item in events]
        self.assertIn("job.blocked", event_types)
        self.assertIn("job.queued", event_types)

    def test_watch_helper_tracks_ordered_events_until_terminal(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_watch", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_watch"},
                "message": {"text": "watch me", "metadata": {}},
                "detach_policy": {"mode": "inline"},
            },
            headers={"Idempotency-Key": "watch-flow-1"},
        ).json()

        snapshots = watch_job_until_terminal(
            self.client,
            job_id=sent["data"]["job_id"],
            poll_interval_seconds=0.0,
            max_polls=2,
        )
        self.assertGreaterEqual(len(snapshots), 1)
        final = snapshots[-1]
        self.assertEqual(final["job"]["status"], "completed")
        event_types = [item["event_type"] for item in final["events"]]
        self.assertIn("job.completed", event_types)
        self.assertEqual(
            sorted(item["event_seq"] for item in final["events"]),
            [item["event_seq"] for item in final["events"]],
        )

    def test_orchestration_helpers_cover_send_list_interrupt_and_fetch(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_orc", "capability_id": "cap_python"})

        sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_orc",
            text="orchestrate me",
            idempotency_key="orc-helper-1",
        )
        job_id = sent["job_id"]

        jobs = list_jobs_via_api(self.client, target_agent_id="agt_orc")
        self.assertTrue(any(item["job_id"] == job_id for item in jobs["items"]))

        agents = list_agents_via_api(self.client, capability_id="cap_python")
        self.assertTrue(any(item["agent_id"] == "agt_orc" for item in agents["items"]))

        interrupted = interrupt_job_via_api(self.client, job_id=job_id)
        self.assertEqual(interrupted["status"], "cancelled")

        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_orc",
            text="inline artifact",
            detach_mode="inline",
            idempotency_key="orc-helper-2",
        )
        artifact = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        content = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertIn("content", content)
        self.assertIn("inline result", content["content"])
        self.assertIn("storage_ref", content)

    def test_inmemory_artifact_backend_supports_inline_result_fetch(self) -> None:
        settings.artifact_backend = "inmemory"
        reset_artifact_store_state("inmemory")
        self.client.post("/agents/up", json={"agent_id": "agt_memart", "capability_id": "cap_python"})

        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_memart",
            text="inline artifact in memory",
            detach_mode="inline",
            idempotency_key="mem-art-1",
        )
        artifact = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("mem://"))
        self.assertNotEqual(artifact["checksum"], "")
        content = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(content["storage_ref"], artifact["storage_ref"])
        self.assertIn("inline result", content["content"])

    def test_sharedfs_artifact_backend_supports_inline_result_fetch(self) -> None:
        settings.artifact_backend = "sharedfs"
        self.client.post("/agents/up", json={"agent_id": "agt_sharedart", "capability_id": "cap_python"})

        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_sharedart",
            text="inline artifact in shared fs",
            detach_mode="inline",
            idempotency_key="shared-art-1",
        )
        artifact = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("agpfs://"))
        self.assertNotEqual(artifact["checksum"], "")
        content = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(content["storage_ref"], artifact["storage_ref"])
        self.assertIn("inline result", content["content"])

    def test_registryfs_artifact_backend_supports_inline_result_fetch(self) -> None:
        settings.artifact_backend = "registryfs"
        self.client.post("/agents/up", json={"agent_id": "agt_registryart", "capability_id": "cap_python"})

        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_registryart",
            text="inline artifact in registry fs",
            detach_mode="inline",
            idempotency_key="registry-art-1",
        )
        artifact = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("agpr://"))
        self.assertNotEqual(artifact["checksum"], "")
        content = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(content["storage_ref"], artifact["storage_ref"])
        self.assertIn("inline result", content["content"])

    def test_localfs_artifact_backend_populates_checksum(self) -> None:
        settings.artifact_backend = "localfs"
        self.client.post("/agents/up", json={"agent_id": "agt_localart", "capability_id": "cap_python"})
        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_localart",
            text="inline artifact in local fs",
            detach_mode="inline",
            idempotency_key="local-art-1",
        )
        artifact = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("file://"))
        self.assertNotEqual(artifact["checksum"], "")

    def test_job_and_run_artifact_listing_exposes_transcript_and_exec_roles(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_artlist", "capability_id": "cap_python"})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_artlist", "hostname": "localhost"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_artlist"},
                "message": {"text": "artifact list run", "metadata": {}},
            },
            headers={"Idempotency-Key": "artifact-list-1"},
        ).json()["data"]
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_artlist", "agent_id": "agt_artlist"},
        ).json()["data"]
        run_id = claim["run"]["run_id"]
        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_artlist",
                "lease_id": claim["lease"]["lease_id"],
                "fencing_token": claim["lease"]["fencing_token"],
                "artifacts": self._materialize_terminal_artifacts(
                    {
                        "prompt.txt": "prompt",
                        "transcript.txt": "transcript_log",
                        "exec.txt": "exec_log",
                        "result.txt": "result",
                    }
                ),
                "summary": {"ok": True},
            },
        )
        self.assertEqual(complete.status_code, 200)

        job_artifacts = list_job_artifacts_via_api(self.client, job_id=sent["job_id"])
        run_artifacts = list_run_artifacts_via_api(self.client, run_id=run_id)
        job_roles = {item["role"] for item in job_artifacts["items"]}
        run_roles = {item["role"] for item in run_artifacts["items"]}
        self.assertIn("transcript_log", job_roles)
        self.assertIn("exec_log", job_roles)
        self.assertIn("transcript_log", run_roles)
        self.assertIn("exec_log", run_roles)

        transcript_only = list_job_artifacts_via_api(self.client, job_id=sent["job_id"], role="transcript_log")
        self.assertEqual(len(transcript_only["items"]), 1)
        self.assertEqual(transcript_only["items"][0]["role"], "transcript_log")
        transcript_content = fetch_artifact_via_api(
            self.client,
            artifact_id=transcript_only["items"][0]["artifact_id"],
            content=True,
        )
        self.assertIn("transcript_log", transcript_content["content"])

    def test_observability_summary_reports_core_counts(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_obs", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_obs"},
                "message": {"text": "queued for metrics", "metadata": {}},
            },
            headers={"Idempotency-Key": "obs-queued-1"},
        )
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_obs"},
                "message": {"text": "inline for metrics", "metadata": {}},
                "detach_policy": {"mode": "inline"},
            },
            headers={"Idempotency-Key": "obs-inline-1"},
        )

        summary = observability_summary_via_api(self.client)
        self.assertGreaterEqual(summary["jobs"]["queued"], 1)
        self.assertGreaterEqual(summary["jobs"]["completed"], 1)
        self.assertGreaterEqual(summary["agents"]["idle"], 1)
        self.assertGreaterEqual(summary["queue"]["depth"], 1)
        self.assertGreater(summary["events"]["latest_event_seq"], 0)

    def test_observability_job_trace_reports_ordered_timeline_and_durations(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_trace", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_trace"},
                "message": {"text": "trace this run", "metadata": {"kind": "trace"}},
            },
            headers={"Idempotency-Key": "trace-flow-1"},
        )
        runtime_client = RuntimeClient(
            RuntimeIdentity(runtime_id="rtm_trace", hostname="localhost", server_url="http://testserver"),
            client=self.client,
        )
        worker = RuntimeSupervisor(
            runtime_client,
            host=InProcessTerminalHost(),
            adapter=DefaultAgentAdapter(),
            artifact_root=".agp-artifacts-tests",
        )
        try:
            payload = worker.run_once(agent_id="agt_trace")
        finally:
            runtime_client.close()

        job_id = payload["claim"]["job"]["job_id"]
        trace = observability_job_trace_via_api(self.client, job_id=job_id)
        event_seqs = [item["event_seq"] for item in trace["timeline"]]
        event_types = [item["event_type"] for item in trace["timeline"]]
        self.assertEqual(event_seqs, sorted(event_seqs))
        self.assertIn("job.accepted", event_types)
        self.assertIn("lease.acquired", event_types)
        self.assertIn("run.completed", event_types)
        self.assertEqual(trace["job"]["job_id"], job_id)
        self.assertGreaterEqual(len(trace["runs"]), 1)
        self.assertIsNotNone(trace["trace"]["durations_seconds"]["total"])

    def test_observability_control_plane_logs_report_structured_lifecycle_entries(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_logs", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_logs"},
                "message": {"text": "log this run", "metadata": {"kind": "logs"}},
            },
            headers={"Idempotency-Key": "logs-flow-1"},
        )
        runtime_client = RuntimeClient(
            RuntimeIdentity(runtime_id="rtm_logs", hostname="localhost", server_url="http://testserver"),
            client=self.client,
        )
        worker = RuntimeSupervisor(
            runtime_client,
            host=InProcessTerminalHost(),
            adapter=DefaultAgentAdapter(),
            artifact_root=".agp-artifacts-tests",
        )
        try:
            payload = worker.run_once(agent_id="agt_logs")
        finally:
            runtime_client.close()

        job_id = payload["claim"]["job"]["job_id"]
        logs = observability_control_plane_logs_via_api(self.client, limit=200)
        self.assertTrue(Path(logs["source"]).name.endswith("control-plane.jsonl"))
        items = logs["items"]
        self.assertGreaterEqual(len(items), 1)
        related = [item for item in items if item.get("job_id") == job_id]
        self.assertTrue(any(item["event_type"] == "job.accepted" for item in related))
        self.assertTrue(any(item["event_type"] == "run.completed" for item in related))
        self.assertTrue(all(item["kind"] == "control_plane_event" for item in related))

    def test_observability_control_plane_logs_span_rotated_files(self) -> None:
        settings.observability_log_rotation_bytes = 1
        self.client.post("/agents/up", json={"agent_id": "agt_logs_rot", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_logs_rot"},
                "message": {"text": "rotate control plane logs", "metadata": {"kind": "logs-rotate"}},
            },
            headers={"Idempotency-Key": "logs-rotate-1"},
        )
        rotated = list(settings.log_root.glob("control-plane.*.jsonl"))
        self.assertGreaterEqual(len(rotated), 1)

        logs = observability_control_plane_logs_via_api(self.client, limit=200)
        event_types = {item["event_type"] for item in logs["items"] if item.get("kind") == "control_plane_event"}
        self.assertIn("agent.provisioning", event_types)
        self.assertIn("agent.idle", event_types)
        self.assertIn("job.accepted", event_types)

    def test_observability_runtime_logs_report_supervision_entries(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_runtime_logs", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_runtime_logs"},
                "message": {"text": "runtime log this run", "metadata": {"kind": "runtime-logs"}},
            },
            headers={"Idempotency-Key": "runtime-logs-flow-1"},
        )
        runtime_client = RuntimeClient(
            RuntimeIdentity(runtime_id="rtm_logs_api", hostname="localhost", server_url="http://testserver"),
            client=self.client,
        )
        worker = RuntimeSupervisor(
            runtime_client,
            host=InProcessTerminalHost(),
            adapter=DefaultAgentAdapter(),
            artifact_root=".agp-artifacts-tests",
        )
        try:
            payload = worker.run_once(agent_id="agt_runtime_logs")
        finally:
            runtime_client.close()

        self.assertTrue(payload["claimed"])
        logs = observability_runtime_logs_via_api(self.client, runtime_id="rtm_logs_api", limit=200)
        self.assertEqual(logs["runtime_id"], "rtm_logs_api")
        self.assertTrue(Path(logs["source"]).name.endswith("runtime-rtm_logs_api.jsonl"))
        items = logs["items"]
        self.assertGreaterEqual(len(items), 1)
        actions = {item["action"] for item in items}
        self.assertIn("register", actions)
        self.assertIn("claim", actions)
        self.assertIn("execution_started", actions)
        self.assertIn("complete", actions)

    def test_logs_prune_removes_only_old_rotated_files(self) -> None:
        settings.observability_control_plane_log_retention_days = 30
        settings.log_root.mkdir(parents=True, exist_ok=True)
        current = settings.log_root / "control-plane.jsonl"
        current.write_text("{\"kind\":\"current\"}\n", encoding="utf-8")
        old_rotated = settings.log_root / "control-plane.20000101T000000000000Z.jsonl"
        old_rotated.write_text("{\"kind\":\"old\"}\n", encoding="utf-8")
        recent_rotated = settings.log_root / "control-plane.29990101T000000000000Z.jsonl"
        recent_rotated.write_text("{\"kind\":\"recent\"}\n", encoding="utf-8")

        import os

        old_ts = (utc_now() - timedelta(days=45)).timestamp()
        recent_ts = utc_now().timestamp()
        os.utime(old_rotated, (old_ts, old_ts))
        os.utime(recent_rotated, (recent_ts, recent_ts))

        pruned = prune_observability_logs()
        self.assertEqual(pruned["control_plane"]["deleted"], 1)
        self.assertEqual(pruned["control_plane"]["kept"], 1)
        self.assertFalse(old_rotated.exists())
        self.assertTrue(recent_rotated.exists())
        self.assertTrue(current.exists())

    def test_observability_alerts_report_dead_letters_and_failure_rate(self) -> None:
        for idx in range(3):
            agent_id = f"agt_alerts_{idx}"
            self.client.post("/agents/up", json={"agent_id": agent_id, "capability_id": "cap_python"})
            sent = self.client.post(
                "/messages/send",
                json={
                    "target": {"type": "agent", "id": agent_id},
                    "message": {"text": f"alert failure {idx}", "metadata": {}},
                },
                headers={"Idempotency-Key": f"alert-fail-{idx}"},
            ).json()["data"]
            self.client.post("/runtimes/register", json={"runtime_id": f"rtm_alert_fail_{idx}", "hostname": "localhost"})
            claim = self.client.post(
                "/runs/claim",
                json={"runtime_id": f"rtm_alert_fail_{idx}", "agent_id": agent_id},
            ).json()["data"]
            self.assertTrue(claim["claimed"])
            artifacts = self._materialize_terminal_artifacts(
                {
                    "prompt.txt": "prompt",
                    "transcript.txt": "transcript_log",
                    "exec.txt": "exec_log",
                    "failure.txt": "failure_evidence",
                }
            )
            failed = self.client.post(
                f"/runs/{claim['run']['run_id']}/fail",
                json={
                    "runtime_id": f"rtm_alert_fail_{idx}",
                    "lease_id": claim["lease"]["lease_id"],
                    "fencing_token": claim["lease"]["fencing_token"],
                    "error": "intentional failure",
                    "artifacts": artifacts,
                    "summary": {"kind": "alert-test"},
                },
            )
            self.assertEqual(failed.status_code, 200)
            self.assertEqual(self.client.get(f"/jobs/{sent['job_id']}").json()["data"]["status"], "failed")

        self.client.post("/agents/up", json={"agent_id": "agt_alerts_dead", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_alerts_dead"},
                "message": {"text": "dead letter this delivery", "metadata": {}},
            },
            headers={"Idempotency-Key": "alert-dead-letter-1"},
        ).json()["data"]
        session = SessionLocal()
        try:
            backend = get_queue_backend(settings.queue_backend)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_alerts_dead"])
            self.assertIsNotNone(delivery)
            assert delivery is not None
            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            record.delivery_attempt = 3
            record.last_delivered_at = utc_now() - timedelta(seconds=60)
            session.commit()
            backend.redrive_stale_deliveries(
                session,
                visibility_timeout_seconds=30,
                max_delivery_attempts=3,
            )
            session.commit()
        finally:
            session.close()

        alerts = observability_alerts_via_api(self.client)
        codes = {item["code"] for item in alerts["items"]}
        self.assertIn("queue_dead_lettering", codes)
        self.assertIn("rising_terminal_failure_rate", codes)
        self.assertGreaterEqual(alerts["counts"]["dead_lettered_deliveries"], 1)

    def test_observability_alert_thresholds_are_configurable(self) -> None:
        settings.observability_unreachable_runtime_threshold = 2
        settings.observability_expired_lease_alert_threshold = 10
        settings.observability_dead_letter_alert_threshold = 2
        settings.observability_terminal_failure_sample_size = 5
        settings.observability_terminal_failure_rate_threshold = 0.9

        self.client.post("/runtimes/register", json={"runtime_id": "rtm_alert_cfg", "hostname": "localhost"})
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_alert_cfg")
            assert runtime is not None
            runtime.health_status = HealthStatus.UNREACHABLE.value
            session.commit()
        finally:
            session.close()

        self.client.post("/agents/up", json={"agent_id": "agt_alert_cfg", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_alert_cfg"},
                "message": {"text": "dead letter this once", "metadata": {}},
            },
            headers={"Idempotency-Key": "alert-config-dead-letter-1"},
        )

        session = SessionLocal()
        try:
            backend = get_queue_backend(settings.queue_backend)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_alert_cfg"])
            self.assertIsNotNone(delivery)
            assert delivery is not None
            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            record.delivery_attempt = 3
            record.last_delivered_at = utc_now() - timedelta(seconds=60)
            session.commit()
            backend.redrive_stale_deliveries(
                session,
                visibility_timeout_seconds=30,
                max_delivery_attempts=3,
            )
            session.commit()
        finally:
            session.close()

        alerts = observability_alerts_via_api(self.client)
        codes = {item["code"] for item in alerts["items"]}
        self.assertNotIn("runtime_unreachable", codes)
        self.assertNotIn("heartbeat_loss_spike", codes)
        self.assertNotIn("repeated_fencing_events", codes)
        self.assertNotIn("queue_dead_lettering", codes)
        self.assertNotIn("rising_terminal_failure_rate", codes)

    def test_backup_and_restore_snapshot_preserves_state_and_artifacts(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_backup", "capability_id": "cap_python"})
        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_backup",
            text="backup this artifact",
            detach_mode="inline",
            idempotency_key="backup-1",
        )
        artifact_before = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertIn("inline result", artifact_before["content"])

        backup_dir = Path(mkdtemp(prefix="agp-backup-"))
        create_backup_snapshot(backup_dir=backup_dir)

        engine.dispose()
        db_path = Path(settings.database_url.removeprefix("sqlite+pysqlite:///"))
        if db_path.exists():
            db_path.unlink()
        if settings.artifact_root.exists():
            shutil.rmtree(settings.artifact_root)
        Base.metadata.create_all(bind=engine)

        missing = self.client.get(f"/jobs/{inline_sent['job_id']}")
        self.assertEqual(missing.status_code, 404)

        restore_backup_snapshot(backup_dir=backup_dir)

        restored_job = self.client.get(f"/jobs/{inline_sent['job_id']}")
        self.assertEqual(restored_job.status_code, 200)
        self.assertEqual(restored_job.json()["data"]["status"], "completed")
        restored_artifact = fetch_artifact_via_api(self.client, artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(restored_artifact["storage_ref"], artifact_before["storage_ref"])
        self.assertIn("inline result", restored_artifact["content"])

    def test_validate_restored_state_reports_missing_artifacts(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_validate", "capability_id": "cap_python"})
        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_validate",
            text="validate restored artifact",
            detach_mode="inline",
            idempotency_key="validate-restore-1",
        )
        artifact_before = fetch_artifact_via_api(
            self.client,
            artifact_id=inline_sent["result_artifact_id"],
            content=True,
        )
        self.assertIn("inline result", artifact_before["content"])
        backup_dir = Path(mkdtemp(prefix="agp-backup-validate-"))
        create_backup_snapshot(backup_dir=backup_dir)

        restore_backup_snapshot(backup_dir=backup_dir)
        shutil.rmtree(settings.artifact_root)
        settings.artifact_root.mkdir(parents=True, exist_ok=True)

        validation = validate_restored_state()
        self.assertFalse(validation["ok"])
        self.assertGreaterEqual(validation["missing_artifacts"], 1)
        missing_ids = {item["artifact_id"] for item in validation["missing"]}
        self.assertIn(inline_sent["result_artifact_id"], missing_ids)

    def test_reconstruct_queue_from_state_rebuilds_pending_delivery_and_dedupes_stale_rows(self) -> None:
        settings.queue_backend = "delivery_table"
        self.client.post("/agents/up", json={"agent_id": "agt_reconstruct", "capability_id": "cap_python"})
        sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_reconstruct",
            text="reconstruct this queue",
            idempotency_key="queue-reconstruct-1",
        )

        session = SessionLocal()
        try:
            stale_rows = session.query(QueueDeliveryRecord).filter_by(job_id=sent["job_id"]).all()
            self.assertEqual(len(stale_rows), 1)
            for row in stale_rows:
                session.delete(row)
            session.add(
                QueueDeliveryRecord(
                    delivery_id="qdl_stale_one",
                    job_id=sent["job_id"],
                    target_queue="agent:agt_reconstruct",
                    state="dead_lettered",
                    delivery_attempt=2,
                    available_at=utc_now(),
                    dead_lettered_at=utc_now(),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            session.add(
                QueueDeliveryRecord(
                    delivery_id="qdl_stale_two",
                    job_id=sent["job_id"],
                    target_queue="agent:agt_reconstruct",
                    state="delivered",
                    delivery_attempt=1,
                    available_at=utc_now(),
                    last_delivered_at=utc_now(),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
            session.commit()
        finally:
            session.close()

        reconstructed = reconstruct_queue_from_state()
        self.assertEqual(reconstructed["queue_backend"], "delivery_table")
        self.assertEqual(reconstructed["reconstructed_jobs"], 1)

        session = SessionLocal()
        try:
            rows = session.query(QueueDeliveryRecord).filter_by(job_id=sent["job_id"]).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].state, "pending")
            self.assertEqual(rows[0].target_queue, "agent:agt_reconstruct")
        finally:
            session.close()

    def test_restore_and_recover_snapshot_rebuilds_queue_and_validates_artifacts(self) -> None:
        settings.queue_backend = "delivery_table"
        self.client.post("/agents/up", json={"agent_id": "agt_dr", "capability_id": "cap_python"})
        inline_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_dr",
            text="dr inline artifact",
            detach_mode="inline",
            idempotency_key="dr-inline-1",
        )
        queued_sent = send_message_via_api(
            self.client,
            target_type="agent",
            target_id="agt_dr",
            text="dr queued work",
            idempotency_key="dr-queued-1",
        )

        backup_dir = Path(mkdtemp(prefix="agp-backup-recover-"))
        create_backup_snapshot(backup_dir=backup_dir)

        engine.dispose()
        db_path = Path(settings.database_url.removeprefix("sqlite+pysqlite:///"))
        if db_path.exists():
            db_path.unlink()
        if settings.artifact_root.exists():
            shutil.rmtree(settings.artifact_root)
        Base.metadata.create_all(bind=engine)

        recovered = restore_and_recover_snapshot(backup_dir=backup_dir)
        self.assertTrue(recovered["ok"])
        self.assertGreaterEqual(recovered["validation"]["checked_artifacts"], 1)
        self.assertEqual(recovered["validation"]["missing_artifacts"], 0)
        self.assertGreaterEqual(recovered["queue_reconstruction"]["reconstructed_jobs"], 1)

        restored_artifact = fetch_artifact_via_api(
            self.client,
            artifact_id=inline_sent["result_artifact_id"],
            content=True,
        )
        self.assertIn("inline result", restored_artifact["content"])

        session = SessionLocal()
        try:
            rows = session.query(QueueDeliveryRecord).filter_by(job_id=queued_sent["job_id"]).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].state, "pending")
        finally:
            session.close()

    def test_failure_injection_lease_expiry_requeue_drill_reports_expected_outcome(self) -> None:
        result = run_failure_injection_scenario(scenario="lease_expiry_requeue")
        self.assertEqual(result["scenario"], "lease_expiry_requeue")
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["sweep"]["expired_leases"], 1)
        self.assertEqual(result["sweep"]["requeued_jobs"], 1)
        self.assertIn("lease.expired", result["event_types"])
        self.assertIn("run.abandoned", result["event_types"])
        self.assertIn("job.requeued", result["event_types"])

    def test_failure_injection_duplicate_terminal_replay_drill_reports_expected_outcome(self) -> None:
        result = run_failure_injection_scenario(scenario="duplicate_terminal_replay")
        self.assertEqual(result["scenario"], "duplicate_terminal_replay")
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["result_artifact_id"])
        self.assertEqual(result["first_terminal_status_code"], 200)
        self.assertEqual(result["second_terminal_status_code"], 409)
        self.assertEqual(result["second_terminal_body"]["ok"], False)
        self.assertEqual(result["second_terminal_body"]["error"]["code"], "conflict")
        self.assertIn("active lease not found", result["second_terminal_body"]["error"]["message"])
        self.assertIn("run.completed", result["event_types"])
        self.assertIn("job.completed", result["event_types"])

    def test_failure_injection_artifact_store_write_failure_drill_reports_expected_outcome(self) -> None:
        result = run_failure_injection_scenario(scenario="artifact_store_write_failure")
        self.assertEqual(result["scenario"], "artifact_store_write_failure")
        self.assertEqual(result["terminal_status_code"], 400)
        self.assertEqual(result["terminal_body"]["ok"], False)
        self.assertEqual(result["terminal_body"]["error"]["code"], "invalid_request")
        self.assertEqual(result["job_status"], "running")
        self.assertEqual(result["run_status"], "running")
        self.assertEqual(result["lease_status"], "active")
        self.assertEqual(result["artifact_count"], 0)

    def test_failure_injection_queue_redelivery_after_consumer_restart_drill_reports_expected_outcome(self) -> None:
        result = run_failure_injection_scenario(scenario="queue_redelivery_after_consumer_restart")
        self.assertEqual(result["scenario"], "queue_redelivery_after_consumer_restart")
        self.assertEqual(result["job_status"], "completed")
        self.assertGreaterEqual(result["redrive"]["redriven_deliveries"], 1)
        self.assertEqual(result["redrive"]["dead_lettered_deliveries"], 0)
        self.assertTrue(result["claim_succeeded"])
        self.assertEqual(result["run_count"], 1)
        self.assertEqual(result["complete_status_code"], 200)
        self.assertIn("run.created", result["event_types"])
        self.assertIn("run.completed", result["event_types"])
        self.assertIn("job.completed", result["event_types"])

    def test_failure_injection_repeated_fencing_stale_owner_drill_reports_expected_outcome(self) -> None:
        result = run_failure_injection_scenario(scenario="repeated_fencing_stale_owner")
        self.assertEqual(result["scenario"], "repeated_fencing_stale_owner")
        self.assertEqual(result["expired_leases"], 3)
        self.assertEqual(result["job_status"], "failed")
        self.assertEqual(len(result["run_ids"]), 3)
        self.assertIn("repeated_fencing_events", result["alert_codes"])
        self.assertIn("heartbeat_loss_spike", result["alert_codes"])
        self.assertEqual(len(result["stale_attempts"]), 3)
        for attempt in result["stale_attempts"]:
            self.assertEqual(attempt["status_code"], 409)
            self.assertEqual(attempt["body"]["ok"], False)
            self.assertEqual(attempt["body"]["error"]["code"], "conflict")
            self.assertIn("active lease not found", attempt["body"]["error"]["message"])

    def test_failure_injection_control_plane_restart_active_work_drill_reports_expected_outcome(self) -> None:
        result = run_failure_injection_scenario(scenario="control_plane_restart_active_work")
        self.assertEqual(result["scenario"], "control_plane_restart_active_work")
        self.assertEqual(result["job_status"], "completed")
        self.assertEqual(result["sweep"]["expired_leases"], 1)
        self.assertEqual(result["sweep"]["requeued_jobs"], 1)
        self.assertEqual(result["run_count"], 2)
        self.assertEqual(result["first_run_status"], "abandoned")
        self.assertEqual(result["second_run_status"], "completed")
        self.assertEqual(result["complete_status_code"], 200)
        self.assertIn("run.abandoned", result["event_types"])
        self.assertIn("job.requeued", result["event_types"])
        self.assertIn("run.completed", result["event_types"])
        self.assertIn("job.completed", result["event_types"])

    def test_upgrade_status_and_mark_persist_previous_versions(self) -> None:
        initial = get_upgrade_status()
        self.assertEqual(initial["schema_version"], "0001_initial")
        self.assertEqual(initial["release_version"], "0.1.0")
        self.assertIsNone(initial["previous_release_version"])

        updated = mark_upgrade(schema_version="0002_queue_backends", release_version="0.2.0")
        self.assertEqual(updated["schema_version"], "0002_queue_backends")
        self.assertEqual(updated["release_version"], "0.2.0")
        self.assertEqual(updated["previous_schema_version"], "0001_initial")
        self.assertEqual(updated["previous_release_version"], "0.1.0")
        self.assertEqual(updated["rollback_target_release_version"], "0.1.0")

    def test_upgrade_rollback_restores_immediately_previous_versions(self) -> None:
        mark_upgrade(schema_version="0002_queue_backends", release_version="0.2.0")
        rolled_back = rollback_to_previous_version()
        self.assertEqual(rolled_back["release_version"], "0.1.0")
        self.assertEqual(rolled_back["schema_version"], "0001_initial")
        self.assertEqual(rolled_back["previous_release_version"], "0.2.0")
        self.assertEqual(rolled_back["previous_schema_version"], "0002_queue_backends")

    def test_upgrade_rollback_rejects_when_no_previous_target_exists(self) -> None:
        with self.assertRaises(typer.BadParameter):
            rollback_to_previous_version()

    def test_upgrade_status_is_exposed_over_operator_api(self) -> None:
        response = self.client.get("/system/upgrade-status")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["schema_version"], "0001_initial")
        self.assertEqual(data["release_version"], "0.1.0")

    def test_runtime_registration_enforces_supported_version_skew(self) -> None:
        equal = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_equal", "hostname": "localhost", "release_version": "0.1.0"},
        )
        self.assertEqual(equal.status_code, 200)
        self.assertEqual(equal.json()["data"]["release_version"], "0.1.0")

        one_minor_behind = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_behind", "hostname": "localhost", "release_version": "0.0.0"},
        )
        self.assertEqual(one_minor_behind.status_code, 200)
        self.assertEqual(one_minor_behind.json()["data"]["release_version"], "0.0.0")

        ahead = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_ahead", "hostname": "localhost", "release_version": "0.2.0"},
        )
        self.assertEqual(ahead.status_code, 409)
        self.assertEqual(ahead.json()["error"]["code"], "conflict")

        too_far_behind = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_old", "hostname": "localhost", "release_version": "0.0.0"},
        )
        self.assertEqual(too_far_behind.status_code, 200)

        mark_upgrade(schema_version="0002_queue_backends", release_version="0.2.0")

        stale_after_upgrade = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_too_old", "hostname": "localhost", "release_version": "0.0.0"},
        )
        self.assertEqual(stale_after_upgrade.status_code, 409)
        self.assertEqual(stale_after_upgrade.json()["error"]["code"], "conflict")

        major_skew = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_major", "hostname": "localhost", "release_version": "1.0.0"},
        )
        self.assertEqual(major_skew.status_code, 409)

    def test_handoff_creates_child_job_that_can_be_claimed(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_parent", "capability_id": "cap_python"})
        self.client.post("/agents/up", json={"agent_id": "agt_child", "capability_id": "cap_python"})

        parent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_parent"},
                "message": {"text": "parent result", "metadata": {}},
                "detach_policy": {"mode": "inline"},
            },
            headers={"Idempotency-Key": "handoff-parent-1"},
        ).json()["data"]

        handoff = self.client.post(
            f"/jobs/{parent['job_id']}/handoff",
            json={
                "artifact_ids": [parent["result_artifact_id"]],
                "targets": [{"type": "agent", "id": "agt_child"}],
                "message": {"text": "continue from parent", "metadata": {}},
            },
        )
        self.assertEqual(handoff.status_code, 200)
        child_job_id = handoff.json()["data"]["child_job_ids"][0]

        self.client.post("/runtimes/register", json={"runtime_id": "rtm_handoff", "hostname": "localhost"})
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_handoff", "agent_id": "agt_child"},
        )
        self.assertEqual(claim.status_code, 200)
        payload = claim.json()["data"]
        self.assertTrue(payload["claimed"])
        self.assertEqual(payload["job"]["job_id"], child_job_id)

    def test_delivery_table_backend_persists_and_acks_deliveries(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_queue", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_queue"},
                "message": {"text": "queue delivery", "metadata": {}},
            },
            headers={"Idempotency-Key": "queue-delivery-1"},
        ).json()["data"]

        session = SessionLocal()
        try:
            pending = session.query(QueueDeliveryRecord).filter_by(job_id=sent["job_id"]).one()
            self.assertEqual(pending.state, "pending")
            delivery_id = pending.delivery_id
        finally:
            session.close()

        self.client.post("/runtimes/register", json={"runtime_id": "rtm_queue", "hostname": "localhost"})
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_queue", "agent_id": "agt_queue"},
        )
        self.assertEqual(claim.status_code, 200)
        self.assertTrue(claim.json()["data"]["claimed"])

        session = SessionLocal()
        try:
            acked = session.get(QueueDeliveryRecord, delivery_id)
            assert acked is not None
            self.assertEqual(acked.state, "acked")
            self.assertEqual(acked.job_id, sent["job_id"])
            self.assertGreaterEqual(acked.delivery_attempt, 1)
        finally:
            session.close()

    def test_delivery_table_backend_redrives_stale_deliveries(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_redrive", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_redrive"},
                "message": {"text": "redrive delivery", "metadata": {}},
            },
            headers={"Idempotency-Key": "queue-redrive-1"},
        ).json()["data"]

        session = SessionLocal()
        try:
            backend = get_queue_backend(settings.queue_backend)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_redrive"])
            self.assertIsNotNone(delivery)
            assert delivery is not None
            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            record.last_delivered_at = utc_now() - timedelta(seconds=60)
            session.commit()

            redriven = backend.redrive_stale_deliveries(
                session,
                visibility_timeout_seconds=30,
                max_delivery_attempts=3,
            )
            session.commit()
            self.assertEqual(redriven["redriven_deliveries"], 1)
            self.assertEqual(redriven["dead_lettered_deliveries"], 0)

            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            self.assertEqual(record.state, "pending")
            self.assertEqual(record.job_id, sent["job_id"])
        finally:
            session.close()

    def test_delivery_table_backend_dead_letters_poison_delivery(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_dead", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_dead"},
                "message": {"text": "dead letter delivery", "metadata": {}},
            },
            headers={"Idempotency-Key": "queue-dead-1"},
        ).json()["data"]

        session = SessionLocal()
        try:
            backend = get_queue_backend(settings.queue_backend)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_dead"])
            self.assertIsNotNone(delivery)
            assert delivery is not None
            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            record.delivery_attempt = 3
            record.last_delivered_at = utc_now() - timedelta(seconds=60)
            session.commit()

            outcome = backend.redrive_stale_deliveries(
                session,
                visibility_timeout_seconds=30,
                max_delivery_attempts=3,
            )
            session.commit()
            self.assertEqual(outcome["redriven_deliveries"], 0)
            self.assertEqual(outcome["dead_lettered_deliveries"], 1)

            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            self.assertEqual(record.state, "dead_lettered")
            self.assertIsNotNone(record.dead_lettered_at)
            self.assertEqual(record.job_id, sent["job_id"])

            redelivery = backend.dequeue_candidate(session, target_queues=["agent:agt_dead"])
            self.assertIsNone(redelivery)
        finally:
            session.close()

        deliveries = list_queue_deliveries_via_api(self.client, state="dead_lettered", job_id=sent["job_id"])
        self.assertEqual(len(deliveries["items"]), 1)
        self.assertEqual(deliveries["items"][0]["job_id"], sent["job_id"])
        self.assertEqual(deliveries["items"][0]["state"], "dead_lettered")

    def test_inmemory_broker_backend_claims_without_db_delivery_table(self) -> None:
        settings.queue_backend = "inmemory_broker"
        reset_queue_backend_state("inmemory_broker")
        self.client.post("/agents/up", json={"agent_id": "agt_memq", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_memq"},
                "message": {"text": "memory broker delivery", "metadata": {}},
            },
            headers={"Idempotency-Key": "queue-memq-1"},
        ).json()["data"]
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_memq", "hostname": "localhost"})

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_memq", "agent_id": "agt_memq"},
        )
        self.assertEqual(claim.status_code, 200)
        self.assertTrue(claim.json()["data"]["claimed"])
        self.assertEqual(claim.json()["data"]["job"]["job_id"], sent["job_id"])

        second = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_memq", "agent_id": "agt_memq"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["data"]["claimed"])

    def test_redis_backend_claims_and_redrives_with_shadow_delivery_records(self) -> None:
        settings.queue_backend = "redis"
        fake_redis = FakeRedisClient()
        queue_backend_module._REDIS_CLIENT_FACTORY = lambda url: fake_redis
        reset_queue_backend_state("redis")

        self.client.post("/agents/up", json={"agent_id": "agt_redis", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_redis"},
                "message": {"text": "redis broker delivery", "metadata": {}},
            },
            headers={"Idempotency-Key": "queue-redis-1"},
        ).json()["data"]

        session = SessionLocal()
        try:
            record = session.query(QueueDeliveryRecord).filter_by(job_id=sent["job_id"]).one()
            self.assertEqual(record.state, "pending")
            backend = get_queue_backend(settings.queue_backend)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_redis"])
            self.assertIsNotNone(delivery)
            assert delivery is not None
            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            self.assertEqual(record.state, "delivered")
            record.last_delivered_at = utc_now() - timedelta(seconds=60)
            session.commit()

            outcome = backend.redrive_stale_deliveries(
                session,
                visibility_timeout_seconds=30,
                max_delivery_attempts=3,
            )
            session.commit()
            self.assertEqual(outcome["redriven_deliveries"], 1)

            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            self.assertEqual(record.state, "pending")
        finally:
            session.close()

        self.client.post("/runtimes/register", json={"runtime_id": "rtm_redis", "hostname": "localhost"})
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_redis", "agent_id": "agt_redis"},
        )
        self.assertEqual(claim.status_code, 200)
        self.assertTrue(claim.json()["data"]["claimed"])
        self.assertEqual(claim.json()["data"]["job"]["job_id"], sent["job_id"])

    def test_standardized_error_envelope_for_not_found_and_conflict(self) -> None:
        missing = self.client.get("/jobs/job_missing")
        self.assertEqual(missing.status_code, 404)
        missing_body = missing.json()
        self.assertEqual(missing_body["ok"], False)
        self.assertEqual(missing_body["error"]["code"], "not_found")

        self.client.post("/agents/up", json={"agent_id": "agt_err", "capability_id": "cap_python"})
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_err", "hostname": "localhost"},
        )
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_err")
            assert runtime is not None
            runtime.status = RuntimeStatus.DRAINING.value
            session.commit()
        finally:
            session.close()
        conflict = self.client.post("/runs/claim", json={"runtime_id": "rtm_err", "agent_id": "agt_err"})
        self.assertEqual(conflict.status_code, 409)
        conflict_body = conflict.json()
        self.assertEqual(conflict_body["ok"], False)
        self.assertEqual(conflict_body["error"]["code"], "conflict")

    def test_auth_middleware_enforces_runtime_and_operator_tokens_when_configured(self) -> None:
        settings.operator_bearer_token = "op-secret"
        settings.runtime_bearer_token = "rt-secret"
        protected_client = TestClient(build_app())
        try:
            op_missing = protected_client.get("/jobs")
            self.assertEqual(op_missing.status_code, 401)
            self.assertEqual(op_missing.json()["error"]["code"], "unauthenticated")

            rt_missing = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_auth", "hostname": "localhost"},
            )
            self.assertEqual(rt_missing.status_code, 401)
            self.assertEqual(rt_missing.json()["error"]["code"], "unauthenticated")

            rt_ok = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_auth", "hostname": "localhost"},
                headers={"Authorization": "Bearer rt-secret"},
            )
            self.assertEqual(rt_ok.status_code, 200)

            op_ok = protected_client.get("/jobs", headers={"Authorization": "Bearer op-secret"})
            self.assertEqual(op_ok.status_code, 200)

            wrong_surface_runtime = protected_client.get("/jobs", headers={"Authorization": "Bearer rt-secret"})
            self.assertEqual(wrong_surface_runtime.status_code, 401)
            self.assertEqual(wrong_surface_runtime.json()["error"]["code"], "unauthenticated")

            wrong_surface_operator = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_auth_2", "hostname": "localhost"},
                headers={"Authorization": "Bearer op-secret"},
            )
            self.assertEqual(wrong_surface_operator.status_code, 401)
            self.assertEqual(wrong_surface_operator.json()["error"]["code"], "unauthenticated")

            health_ok = protected_client.get("/health")
            self.assertEqual(health_ok.status_code, 200)
        finally:
            protected_client.close()
            settings.operator_bearer_token = None
            settings.runtime_bearer_token = None

    def test_operator_rbac_enforces_read_only_vs_lifecycle_roles(self) -> None:
        settings.operator_bearer_token = None
        settings.operator_token_roles_json = {
            "viewer-token": "read_only",
            "lifecycle-token": "lifecycle",
        }
        protected_client = TestClient(build_app())
        try:
            viewer_headers = {"Authorization": "Bearer viewer-token"}
            lifecycle_headers = {"Authorization": "Bearer lifecycle-token"}

            viewer_get = protected_client.get("/capabilities", headers=viewer_headers)
            self.assertEqual(viewer_get.status_code, 200)

            viewer_send = protected_client.post(
                "/messages/send",
                json={
                    "target": {"type": "capability", "id": "cap_python"},
                    "message": {"text": "viewer should not send", "metadata": {}},
                },
                headers=viewer_headers,
            )
            self.assertEqual(viewer_send.status_code, 403)
            self.assertEqual(viewer_send.json()["error"]["code"], "forbidden")

            viewer_up = protected_client.post(
                "/agents/up",
                json={"agent_id": "agt_rbac_blocked", "capability_id": "cap_python"},
                headers=viewer_headers,
            )
            self.assertEqual(viewer_up.status_code, 403)

            lifecycle_send = protected_client.post(
                "/messages/send",
                json={
                    "target": {"type": "capability", "id": "cap_python"},
                    "message": {"text": "lifecycle can send", "metadata": {}},
                },
                headers={**lifecycle_headers, "Idempotency-Key": "rbac-send-1"},
            )
            self.assertEqual(lifecycle_send.status_code, 200)

            lifecycle_up = protected_client.post(
                "/agents/up",
                json={"agent_id": "agt_rbac_ok", "capability_id": "cap_python"},
                headers=lifecycle_headers,
            )
            self.assertEqual(lifecycle_up.status_code, 200)
        finally:
            protected_client.close()
            settings.operator_token_roles_json = {}

    def test_secret_rotation_supports_overlapping_and_revoked_runtime_and_operator_tokens(self) -> None:
        settings.operator_bearer_token = None
        settings.operator_token_roles_json = {
            "op-old": "security_admin",
            "op-new": "security_admin",
        }
        settings.runtime_bearer_token = None
        settings.runtime_active_tokens_json = ["rt-old", "rt-new"]
        protected_client = TestClient(build_app())
        try:
            op_old = protected_client.get("/jobs", headers={"Authorization": "Bearer op-old"})
            self.assertEqual(op_old.status_code, 200)
            op_new = protected_client.get("/jobs", headers={"Authorization": "Bearer op-new"})
            self.assertEqual(op_new.status_code, 200)

            rt_old = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_rot_old", "hostname": "localhost"},
                headers={"Authorization": "Bearer rt-old"},
            )
            self.assertEqual(rt_old.status_code, 200)
            rt_new = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_rot_new", "hostname": "localhost"},
                headers={"Authorization": "Bearer rt-new"},
            )
            self.assertEqual(rt_new.status_code, 200)

            settings.operator_token_roles_json.pop("op-old")
            settings.runtime_active_tokens_json = ["rt-new"]

            op_old_revoked = protected_client.get("/jobs", headers={"Authorization": "Bearer op-old"})
            self.assertEqual(op_old_revoked.status_code, 401)
            op_new_still_valid = protected_client.get("/jobs", headers={"Authorization": "Bearer op-new"})
            self.assertEqual(op_new_still_valid.status_code, 200)

            rt_old_revoked = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_rot_old_revoked", "hostname": "localhost"},
                headers={"Authorization": "Bearer rt-old"},
            )
            self.assertEqual(rt_old_revoked.status_code, 401)
            rt_new_still_valid = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_rot_new_valid", "hostname": "localhost"},
                headers={"Authorization": "Bearer rt-new"},
            )
            self.assertEqual(rt_new_still_valid.status_code, 200)
        finally:
            protected_client.close()
            settings.operator_token_roles_json = {}
            settings.runtime_active_tokens_json = []

    def test_security_admin_surfaces_report_status_rotate_tokens_and_audit(self) -> None:
        settings.operator_bearer_token = None
        settings.operator_token_roles_json = {
            "viewer-token": "read_only",
            "admin-token": "security_admin",
        }
        settings.runtime_bearer_token = None
        settings.runtime_active_tokens_json = ["rt-old"]
        protected_client = TestClient(build_app())
        try:
            viewer_headers = {"Authorization": "Bearer viewer-token"}
            admin_headers = {"Authorization": "Bearer admin-token"}

            denied = protected_client.get("/system/auth-status", headers=viewer_headers)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["error"]["code"], "forbidden")

            protected_client.headers.update(admin_headers)
            status_before = system_auth_status_via_api(protected_client)
            self.assertEqual(status_before["operator"]["managed_token_count"], 2)
            self.assertEqual(status_before["runtime"]["active_token_count"], 1)

            rotated_operator = rotate_operator_tokens_via_api(
                protected_client,
                operator_bearer_token=None,
                operator_token_roles_json={
                    "viewer2": "read_only",
                    "ops2": "operator",
                    "admin2": "security_admin",
                },
            )
            self.assertEqual(rotated_operator["managed_token_count"], 3)
            protected_client.headers.update({"Authorization": "Bearer admin2"})

            rotated_runtime = rotate_runtime_tokens_via_api(
                protected_client,
                runtime_bearer_token=None,
                runtime_active_tokens_json=["rt-new-1", "rt-new-2"],
            )
            self.assertEqual(rotated_runtime["active_token_count"], 2)
            protected_client.headers.clear()

            old_admin = protected_client.get("/system/auth-status", headers=admin_headers)
            self.assertEqual(old_admin.status_code, 401)

            new_admin_headers = {"Authorization": "Bearer admin2"}
            status_after = protected_client.get("/system/auth-status", headers=new_admin_headers)
            self.assertEqual(status_after.status_code, 200)
            payload = status_after.json()["data"]
            self.assertEqual(payload["operator"]["managed_token_count"], 3)
            self.assertEqual(payload["runtime"]["active_token_count"], 2)

            runtime_old = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_rot_status_old", "hostname": "localhost"},
                headers={"Authorization": "Bearer rt-old"},
            )
            self.assertEqual(runtime_old.status_code, 401)

            runtime_new = protected_client.post(
                "/runtimes/register",
                json={"runtime_id": "rtm_rot_status_new", "hostname": "localhost"},
                headers={"Authorization": "Bearer rt-new-1"},
            )
            self.assertEqual(runtime_new.status_code, 200)

            session = SessionLocal()
            try:
                types = [
                    row[0]
                    for row in session.execute(
                        select(Event.event_type)
                        .where(
                            Event.event_type.in_(
                                ("system.operator_tokens_rotated", "system.runtime_tokens_rotated")
                            )
                        )
                        .order_by(Event.event_seq.asc())
                    ).all()
                ]
            finally:
                session.close()
            self.assertIn("system.operator_tokens_rotated", types)
            self.assertIn("system.runtime_tokens_rotated", types)

            restarted_client = TestClient(build_app())
            try:
                restarted_status = restarted_client.get("/system/auth-status", headers=new_admin_headers)
                self.assertEqual(restarted_status.status_code, 200)
                restarted_payload = restarted_status.json()["data"]
                self.assertEqual(restarted_payload["operator"]["managed_token_count"], 3)
                self.assertEqual(restarted_payload["runtime"]["active_token_count"], 2)

                restarted_runtime = restarted_client.post(
                    "/runtimes/register",
                    json={"runtime_id": "rtm_rot_status_restart", "hostname": "localhost"},
                    headers={"Authorization": "Bearer rt-new-2"},
                )
                self.assertEqual(restarted_runtime.status_code, 200)
            finally:
                restarted_client.close()
        finally:
            protected_client.close()
            session = SessionLocal()
            try:
                for key in (
                    "operator_bearer_token",
                    "operator_token_roles_json",
                    "runtime_bearer_token",
                    "runtime_active_tokens_json",
                ):
                    row = session.get(control_plane_module.SystemMetadata, key)
                    if row is not None:
                        session.delete(row)
                session.commit()
            finally:
                session.close()
            settings.operator_bearer_token = None
            settings.operator_token_roles_json = {}
            settings.runtime_bearer_token = None
            settings.runtime_active_tokens_json = []

    def test_explicit_runtime_failure_is_terminal(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_fail", "capability_id": "cap_python"})
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_fail", "hostname": "localhost"},
        )
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_fail"},
                "message": {"text": "fail me", "metadata": {}},
            },
            headers={"Idempotency-Key": "explicit-fail-flow-1"},
        ).json()
        job_id = sent["data"]["job_id"]
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_fail", "agent_id": "agt_fail"},
        ).json()["data"]
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]
        failed = self.client.post(
            f"/runs/{run_id}/fail",
            json={
                "runtime_id": "rtm_fail",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "error": "explicit failure",
                "artifacts": self._materialize_terminal_artifacts(
                    {
                        "prompt-fail.txt": "prompt",
                        "transcript-fail.txt": "transcript_log",
                        "exec-fail.txt": "exec_log",
                        "failure.txt": "failure_evidence",
                    }
                ),
                "summary": {"kind": "explicit"},
            },
        )
        self.assertEqual(failed.status_code, 200)
        payload = failed.json()["data"]
        self.assertEqual(payload["job_status"], "failed")
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "failed")

    def test_complete_rejects_missing_artifact_refs(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_missing_art", "capability_id": "cap_python"})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_missing_art", "hostname": "localhost"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_missing_art"},
                "message": {"text": "bad artifacts", "metadata": {}},
            },
            headers={"Idempotency-Key": "missing-artifacts-1"},
        ).json()["data"]
        job_id = sent["job_id"]
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_missing_art", "agent_id": "agt_missing_art"},
        ).json()["data"]
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_missing_art",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "artifacts": [
                    {"role": "prompt", "storage_ref": "file:///tmp/does-not-exist-prompt.txt", "content_type": "text/plain", "checksum": "p", "size_bytes": 1},
                    {"role": "transcript_log", "storage_ref": "file:///tmp/does-not-exist-transcript.txt", "content_type": "text/plain", "checksum": "t", "size_bytes": 1},
                    {"role": "exec_log", "storage_ref": "file:///tmp/does-not-exist-exec.txt", "content_type": "text/plain", "checksum": "e", "size_bytes": 1},
                    {"role": "result", "storage_ref": "file:///tmp/does-not-exist-result.txt", "content_type": "text/plain", "checksum": "r", "size_bytes": 1},
                ],
                "summary": {"ok": True},
            },
        )
        self.assertEqual(complete.status_code, 400)
        self.assertEqual(complete.json()["error"]["code"], "invalid_request")

        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "running")

    def test_interrupt_requested_run_becomes_cancelled(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_interrupt", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_interrupt"},
                "message": {"text": "interrupt me", "metadata": {}},
            },
            headers={"Idempotency-Key": "interrupt-flow-1"},
        ).json()
        job_id = sent["data"]["job_id"]
        runtime_client = RuntimeClient(
            RuntimeIdentity(runtime_id="rtm_interrupt", hostname="localhost", server_url="http://testserver"),
            client=self.client,
        )
        worker = RuntimeSupervisor(
            runtime_client,
            host=InProcessTerminalHost(),
            adapter=DefaultAgentAdapter(),
            artifact_root=".agp-artifacts-tests",
        )
        holder: dict[str, dict] = {}

        def run_worker() -> None:
            holder["payload"] = worker.run_once(
                agent_id="agt_interrupt",
                heartbeat_interval_seconds=0.01,
            )

        thread = Thread(target=run_worker)
        thread.start()
        sleep(0.03)
        interrupt = self.client.post(f"/jobs/{job_id}/interrupt")
        self.assertEqual(interrupt.status_code, 200)
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

        payload = holder["payload"]
        self.assertTrue(payload["cancelled"])
        self.assertEqual(payload["result"]["status"], "cancelled")

        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "cancelled")
        events = self.client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
        event_types = [item["event_type"] for item in events]
        self.assertIn("job.interrupt_requested", event_types)
        self.assertIn("run.cancelled", event_types)
        self.assertIn("job.cancelled", event_types)

    def test_expired_lease_requeues_job(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_expire", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_expire"},
                "message": {"text": "expire me", "metadata": {}},
            },
            headers={"Idempotency-Key": "expiry-flow-1"},
        ).json()
        job_id = sent["data"]["job_id"]
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_expire", "hostname": "localhost"},
        )
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_expire", "agent_id": "agt_expire", "lease_ttl_seconds": 1},
        ).json()["data"]

        session = SessionLocal()
        try:
            result = sweep_expired_leases(
                session,
                now=utc_now().replace(microsecond=0) + timedelta(seconds=2),
            )
        finally:
            session.close()

        self.assertEqual(result["expired_leases"], 1)
        self.assertEqual(result["requeued_jobs"], 1)
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["retry_count"], 1)
        events = self.client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
        event_types = [item["event_type"] for item in events]
        self.assertIn("lease.expired", event_types)
        self.assertIn("run.abandoned", event_types)
        self.assertIn("job.requeued", event_types)

    def test_idle_timeout_terminates_truly_idle_agent(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_idle", "capability_id": "cap_python"})
        session = SessionLocal()
        try:
            from agp.models import Agent

            agent = session.get(Agent, "agt_idle")
            assert agent is not None
            agent.last_seen_at = utc_now() - timedelta(seconds=600)
            session.commit()
            result = sweep_idle_agents(session, now=utc_now(), idle_timeout_seconds=300)
        finally:
            session.close()

        self.assertEqual(result["terminated_agents"], 1)
        agents = list_agents_via_api(self.client, status="terminated")
        self.assertTrue(any(item["agent_id"] == "agt_idle" for item in agents["items"]))

    def test_idle_timeout_does_not_terminate_agent_with_queued_work(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_idle_queued", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_idle_queued"},
                "message": {"text": "stay queued", "metadata": {}},
            },
            headers={"Idempotency-Key": "idle-queued-1"},
        )
        session = SessionLocal()
        try:
            from agp.models import Agent

            agent = session.get(Agent, "agt_idle_queued")
            assert agent is not None
            agent.last_seen_at = utc_now() - timedelta(seconds=600)
            session.commit()
            result = sweep_idle_agents(session, now=utc_now(), idle_timeout_seconds=300)
        finally:
            session.close()

        self.assertEqual(result["terminated_agents"], 0)
        agent = self.client.get("/agents", params={"status": "idle"}).json()["data"]["items"]
        self.assertTrue(any(item["agent_id"] == "agt_idle_queued" for item in agent))

    def test_idle_timeout_does_not_terminate_agent_with_active_lease(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_idle_lease", "capability_id": "cap_python"})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_idle_lease", "hostname": "localhost"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_idle_lease"},
                "message": {"text": "hold lease", "metadata": {}},
            },
            headers={"Idempotency-Key": "idle-lease-1"},
        )
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_idle_lease", "agent_id": "agt_idle_lease", "lease_ttl_seconds": 300},
        )
        self.assertEqual(claim.status_code, 200)

        session = SessionLocal()
        try:
            from agp.models import Agent

            agent = session.get(Agent, "agt_idle_lease")
            assert agent is not None
            agent.status = "idle"
            agent.last_seen_at = utc_now() - timedelta(seconds=600)
            session.commit()
            result = sweep_idle_agents(session, now=utc_now(), idle_timeout_seconds=300)
        finally:
            session.close()

        self.assertEqual(result["terminated_agents"], 0)

    def test_draining_agent_terminates_when_queue_and_leases_clear(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_drain_done", "capability_id": "cap_python"})
        down = self.client.post("/agents/agt_drain_done/down", json={"mode": "drain"})
        self.assertEqual(down.status_code, 200)

        session = SessionLocal()
        try:
            result = sweep_draining_agents(session, now=utc_now())
        finally:
            session.close()

        self.assertEqual(result["terminated_agents"], 1)
        agents = list_agents_via_api(self.client, status="terminated")
        self.assertTrue(any(item["agent_id"] == "agt_drain_done" for item in agents["items"]))

        session = SessionLocal()
        try:
            events = session.query(Event).filter(Event.agent_id == "agt_drain_done").all()
            self.assertTrue(any(evt.event_type == "agent.terminated" for evt in events))
        finally:
            session.close()

    def test_draining_agent_not_terminated_while_queued_work_exists(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_drain_queue", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_drain_queue"},
                "message": {"text": "stay queued while draining", "metadata": {}},
            },
            headers={"Idempotency-Key": "drain-queue-1"},
        )
        down = self.client.post("/agents/agt_drain_queue/down", json={"mode": "drain"})
        self.assertEqual(down.status_code, 200)

        session = SessionLocal()
        try:
            result = sweep_draining_agents(session, now=utc_now())
        finally:
            session.close()

        self.assertEqual(result["terminated_agents"], 0)
        agents = list_agents_via_api(self.client, status="draining")
        self.assertTrue(any(item["agent_id"] == "agt_drain_queue" for item in agents["items"]))

    def test_draining_runtime_returns_to_idle_when_leases_clear(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_drain_done", "hostname": "localhost"})
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_drain_done")
            assert runtime is not None
            runtime.status = RuntimeStatus.DRAINING.value
            runtime.health_status = HealthStatus.DRAINING.value
            session.commit()
            result = sweep_draining_runtimes(session, now=utc_now())
        finally:
            session.close()

        self.assertEqual(result["resumed_runtimes"], 1)
        runtimes = self.client.get("/runtimes", params={"status": "idle", "health_status": "healthy"}).json()["data"]["items"]
        self.assertTrue(any(item["runtime_id"] == "rtm_drain_done" for item in runtimes))

    def test_draining_runtime_stays_draining_with_active_lease(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_drain_busy", "hostname": "localhost"})
        self.client.post("/agents/up", json={"agent_id": "agt_drain_busy", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_drain_busy"},
                "message": {"text": "keep runtime busy while draining", "metadata": {}},
            },
            headers={"Idempotency-Key": "runtime-drain-busy-1"},
        )
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_drain_busy", "agent_id": "agt_drain_busy", "lease_ttl_seconds": 300},
        )
        self.assertEqual(claim.status_code, 200)

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_drain_busy")
            assert runtime is not None
            runtime.status = RuntimeStatus.DRAINING.value
            runtime.health_status = HealthStatus.DRAINING.value
            session.commit()
            result = sweep_draining_runtimes(session, now=utc_now())
        finally:
            session.close()

        self.assertEqual(result["resumed_runtimes"], 0)
        runtimes = self.client.get("/runtimes", params={"status": "draining"}).json()["data"]["items"]
        self.assertTrue(any(item["runtime_id"] == "rtm_drain_busy" for item in runtimes))

    def test_stale_runtime_offlines_and_detaches_idle_agent(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_stale_idle", "hostname": "localhost"})
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_stale_idle", "capability_id": "cap_python", "assigned_runtime_id": "rtm_stale_idle"},
        )

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_stale_idle")
            agent = session.get(Agent, "agt_stale_idle")
            assert runtime is not None
            assert agent is not None
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=600)
            agent.status = "degraded"
            session.commit()
            result = sweep_stale_runtimes(session, now=utc_now(), stale_timeout_seconds=90)
        finally:
            session.close()

        self.assertEqual(result["offline_runtimes"], 1)
        self.assertEqual(result["detached_agents"], 1)
        runtimes = self.client.get("/runtimes", params={"status": "offline"}).json()["data"]["items"]
        self.assertTrue(any(item["runtime_id"] == "rtm_stale_idle" for item in runtimes))
        agents = self.client.get("/agents", params={"status": "idle"}).json()["data"]["items"]
        stale_agent = next(item for item in agents if item["agent_id"] == "agt_stale_idle")
        self.assertIsNone(stale_agent["assigned_runtime_id"])

        session = SessionLocal()
        try:
            events = session.query(Event).filter(Event.runtime_id == "rtm_stale_idle").all()
            self.assertTrue(any(evt.event_type == "runtime.offline" for evt in events))
        finally:
            session.close()

    def test_stale_runtime_degrades_agent_with_active_lease(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_stale_busy", "hostname": "localhost"})
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_stale_busy", "capability_id": "cap_python", "assigned_runtime_id": "rtm_stale_busy"},
        )
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_stale_busy"},
                "message": {"text": "hold lease while runtime goes stale", "metadata": {}},
            },
            headers={"Idempotency-Key": "stale-runtime-busy-1"},
        )
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_stale_busy", "agent_id": "agt_stale_busy", "lease_ttl_seconds": 300},
        )
        self.assertEqual(claim.status_code, 200)

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_stale_busy")
            assert runtime is not None
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=600)
            session.commit()
            result = sweep_stale_runtimes(session, now=utc_now(), stale_timeout_seconds=90)
        finally:
            session.close()

        self.assertEqual(result["offline_runtimes"], 1)
        self.assertEqual(result["degraded_agents"], 1)
        agents = self.client.get("/agents", params={"status": "degraded"}).json()["data"]["items"]
        stale_agent = next(item for item in agents if item["agent_id"] == "agt_stale_busy")
        self.assertEqual(stale_agent["assigned_runtime_id"], "rtm_stale_busy")

        session = SessionLocal()
        try:
            events = session.query(Event).filter(Event.agent_id == "agt_stale_busy").all()
            self.assertTrue(any(evt.event_type == "agent.degraded" for evt in events))
        finally:
            session.close()

        self.client.post("/runtimes/register", json={"runtime_id": "rtm_takeover", "hostname": "localhost"})
        takeover = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_takeover", "agent_id": "agt_stale_busy", "lease_ttl_seconds": 300},
        )
        self.assertEqual(takeover.status_code, 200)
        self.assertFalse(takeover.json()["data"]["claimed"])

    def test_runtime_sweeper_service_runs_stale_runtime_sweep(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_service", "hostname": "localhost"})
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_service", "capability_id": "cap_python", "assigned_runtime_id": "rtm_service"},
        )
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_service")
            assert runtime is not None
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=600)
            session.commit()
        finally:
            session.close()

        service = SweeperService(
            session_factory=SessionLocal,
            sweep_fn=lambda session: sweep_stale_runtimes(session, stale_timeout_seconds=90),
            interval_seconds=0.01,
        )
        results = service.run_forever(max_iterations=1)
        self.assertEqual(results[0]["offline_runtimes"], 1)

        runtimes = self.client.get("/runtimes", params={"status": "offline"}).json()["data"]["items"]
        self.assertTrue(any(item["runtime_id"] == "rtm_service" for item in runtimes))

    def test_detached_agent_can_reprovision_to_new_runtime_after_stale_sweep(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_old", "hostname": "localhost"})
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_reprov", "capability_id": "cap_python", "assigned_runtime_id": "rtm_old"},
        )

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_old")
            assert runtime is not None
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=600)
            session.commit()
            result = sweep_stale_runtimes(session, now=utc_now(), stale_timeout_seconds=90)
        finally:
            session.close()

        self.assertEqual(result["offline_runtimes"], 1)
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_new", "hostname": "localhost"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_reprov"},
                "message": {"text": "run after reprovision", "metadata": {}},
            },
            headers={"Idempotency-Key": "reprov-1"},
        ).json()
        self.assertIn("job_id", sent["data"])

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_new", "agent_id": "agt_reprov", "lease_ttl_seconds": 300},
        )
        self.assertEqual(claim.status_code, 200)
        self.assertEqual(claim.json()["data"]["agent_id"], "agt_reprov")

        agents = self.client.get("/agents", params={"status": "busy"}).json()["data"]["items"]
        rebound_agent = next(item for item in agents if item["agent_id"] == "agt_reprov")
        self.assertEqual(rebound_agent["assigned_runtime_id"], "rtm_new")

    def test_expired_lease_exhausts_retry_budget_and_fails_job(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_exhaust", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_exhaust"},
                "message": {"text": "fail after retries", "metadata": {}},
            },
            headers={"Idempotency-Key": "expiry-fail-flow-1"},
        ).json()
        job_id = sent["data"]["job_id"]
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_exhaust", "hostname": "localhost"},
        )
        self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_exhaust", "agent_id": "agt_exhaust", "lease_ttl_seconds": 1},
        )

        session = SessionLocal()
        try:
            from agp.models import Job

            job = session.get(Job, job_id)
            assert job is not None
            job.retry_count = 2
            session.commit()
            result = sweep_expired_leases(
                session,
                now=utc_now().replace(microsecond=0) + timedelta(seconds=2),
            )
        finally:
            session.close()

        self.assertEqual(result["expired_leases"], 1)
        self.assertEqual(result["failed_jobs"], 1)
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["retry_count"], 3)
        events = self.client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
        event_types = [item["event_type"] for item in events]
        self.assertIn("job.failed", event_types)

    def test_claim_rejected_for_draining_or_unreachable_runtime(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_blocked", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_blocked"},
                "message": {"text": "blocked claim", "metadata": {}},
            },
            headers={"Idempotency-Key": "blocked-claim-flow-1"},
        )
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_blocked", "hostname": "localhost"},
        )

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_blocked")
            assert runtime is not None
            runtime.status = RuntimeStatus.DRAINING.value
            session.commit()
        finally:
            session.close()

        draining = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_blocked", "agent_id": "agt_blocked"},
        )
        self.assertEqual(draining.status_code, 409)

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_blocked")
            assert runtime is not None
            runtime.status = RuntimeStatus.IDLE.value
            runtime.health_status = HealthStatus.UNREACHABLE.value
            session.commit()
        finally:
            session.close()

        unreachable = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_blocked", "agent_id": "agt_blocked"},
        )
        self.assertEqual(unreachable.status_code, 409)

    def test_capability_targeted_job_claims_with_eligible_runtime_affinity(self) -> None:
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_cap_one", "hostname": "localhost"},
        )
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_cap_two", "hostname": "localhost"},
        )
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_cap_foreign", "capability_id": "cap_python", "assigned_runtime_id": "rtm_cap_two"},
        )
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_cap_local", "capability_id": "cap_python", "assigned_runtime_id": "rtm_cap_one"},
        )
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "capability", "id": "cap_python"},
                "message": {"text": "capability dispatch", "metadata": {}},
            },
            headers={"Idempotency-Key": "cap-claim-flow-1"},
        ).json()

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_cap_one", "capability_id": "cap_python"},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]
        self.assertTrue(claim_data["claimed"])
        self.assertEqual(claim_data["agent_id"], "agt_cap_local")
        self.assertEqual(claim_data["job"]["job_id"], sent["data"]["job_id"])
        self.assertEqual(claim_data["job"]["target_queue"], "capability:cap_python:v1")

    def test_agent_affinity_blocks_wrong_runtime_claim(self) -> None:
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_right", "hostname": "localhost"},
        )
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_wrong", "hostname": "localhost"},
        )
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_affined", "capability_id": "cap_python", "assigned_runtime_id": "rtm_right"},
        )
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_affined"},
                "message": {"text": "affinity job", "metadata": {}},
            },
            headers={"Idempotency-Key": "agent-affinity-flow-1"},
        )

        wrong = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_wrong", "agent_id": "agt_affined"},
        )
        self.assertEqual(wrong.status_code, 200)
        self.assertFalse(wrong.json()["data"]["claimed"])

        right = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_right", "agent_id": "agt_affined"},
        )
        self.assertEqual(right.status_code, 200)
        self.assertTrue(right.json()["data"]["claimed"])

    def test_queued_job_with_exhausted_retries_is_not_claimable(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_retry_cap", "capability_id": "cap_python"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_retry_cap"},
                "message": {"text": "retry budget spent", "metadata": {}},
            },
            headers={"Idempotency-Key": "retry-budget-cap-flow-1"},
        ).json()
        job_id = sent["data"]["job_id"]
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_retry_cap", "hostname": "localhost"},
        )

        session = SessionLocal()
        try:
            from agp.models import Job

            job = session.get(Job, job_id)
            assert job is not None
            job.retry_count = job.max_retries
            session.commit()
        finally:
            session.close()

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_retry_cap", "agent_id": "agt_retry_cap"},
        )
        self.assertEqual(claim.status_code, 200)
        self.assertFalse(claim.json()["data"]["claimed"])
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "failed")

    def test_run_can_transition_through_recovering_and_resumed(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_recover", "capability_id": "cap_python"})
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_recover", "hostname": "localhost"},
        )
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_recover"},
                "message": {"text": "recover path", "metadata": {}},
            },
            headers={"Idempotency-Key": "recovering-flow-1"},
        )
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_recover", "agent_id": "agt_recover"},
        ).json()["data"]
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        self.client.post(
            f"/runs/{run_id}/heartbeat",
            json={"runtime_id": "rtm_recover", "lease_id": lease_id, "fencing_token": fencing_token},
        )
        recovering = self.client.post(
            f"/runs/{run_id}/recovering",
            json={
                "runtime_id": "rtm_recover",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "details": {"reason": "cli_restart"},
            },
        )
        self.assertEqual(recovering.status_code, 200)
        self.assertEqual(recovering.json()["data"]["status"], "recovering")
        recovering_event = recovering.json()["data"]["event_id"]

        resumed = self.client.post(
            f"/runs/{run_id}/resumed",
            json={
                "runtime_id": "rtm_recover",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "details": {"result": "ok"},
            },
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["data"]["status"], "running")
        events = self.client.get(f"/jobs/{claim['job']['job_id']}/events").json()["data"]["items"]
        recovering_body = next(item["body"] for item in events if item["event_id"] == recovering_event)
        self.assertIn("expires_at", recovering_body)

    def test_runtime_worker_recovery_budget_elapsed_time_escalates_to_failure(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_recover_budget", "capability_id": "cap_python"})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_recover_budget"},
                "message": {"text": "budget me", "metadata": {}},
            },
            headers={"Idempotency-Key": "recover-budget-1"},
        )

        runtime_client = RuntimeClient(
            RuntimeIdentity(runtime_id="rtm_recover_budget", hostname="localhost", server_url="http://testserver"),
            client=self.client,
        )
        state = {"calls": 0}

        def execute(_: dict) -> None:
            state["calls"] += 1
            if state["calls"] <= 2:
                raise RecoverableExecutionError("temporary cli failure")
            return None

        def recover(_: dict, *, attempt: int, error: Exception) -> None:  # noqa: ARG001
            sleep(0.03)

        worker = RuntimeSupervisor(
            runtime_client,
            host=InProcessTerminalHost(),
            adapter=DefaultAgentAdapter(execute=execute, recover=recover),
            artifact_root=".agp-artifacts-tests",
        )

        try:
            payload = worker.run_once(
                agent_id="agt_recover_budget",
                heartbeat_interval_seconds=0.01,
                max_local_recoveries=3,
                max_local_recovery_seconds=0.01,
            )
        finally:
            runtime_client.close()

        self.assertTrue(payload["claimed"])
        self.assertEqual(payload["result"]["job_status"], "failed")
        run_id = payload["claim"]["run"]["run_id"]
        artifacts = self.client.get(f"/runs/{run_id}/artifacts").json()["data"]["items"]
        snapshot_roles = [item["role"] for item in artifacts if item["role"] == "failure_evidence"]
        self.assertGreaterEqual(len(snapshot_roles), 3)
        refs = [item["storage_ref"] for item in artifacts if item["role"] == "failure_evidence"]
        self.assertTrue(any(ref.endswith("session-snapshot.json") for ref in refs))
        self.assertTrue(any(ref.endswith("session-health.json") for ref in refs))

    # ── Gap-closure tests: durable output checkpointing ──────────────

    def test_compute_output_delta_prefix_match(self) -> None:
        delta = _compute_output_delta("line1\nline2\nline3\n", "line1\nline2\n")
        self.assertEqual(delta, "line3\n")

    def test_compute_output_delta_scrollback_shift(self) -> None:
        prior = "line1\nline2\nline3\nline4\nline5\n"
        current = "line3\nline4\nline5\nline6\nline7\n"
        delta = _compute_output_delta(current, prior)
        self.assertEqual(delta, "line6\nline7\n")

    def test_compute_output_delta_no_overlap_returns_full_text(self) -> None:
        delta = _compute_output_delta("completely\nnew\n", "totally\nold\n")
        self.assertEqual(delta, "completely\nnew\n")

    def test_compute_output_delta_empty_prior(self) -> None:
        delta = _compute_output_delta("hello\n", "")
        self.assertEqual(delta, "hello\n")

    def test_compute_output_delta_identical(self) -> None:
        delta = _compute_output_delta("same\n", "same\n")
        self.assertEqual(delta, "")

    def test_output_accumulator_persists_and_restores(self) -> None:
        tmp = Path(mkdtemp())
        try:
            path = tmp / "test-session.output.txt"
            acc1 = _OutputAccumulator(path)
            acc1.append("chunk1\n")
            acc1.append("chunk2\n")
            self.assertEqual(acc1.text, "chunk1\nchunk2\n")
            acc2 = _OutputAccumulator(path)
            self.assertEqual(acc2.text, "chunk1\nchunk2\n")
            acc2.append("chunk3\n")
            self.assertEqual(acc2.text, "chunk1\nchunk2\nchunk3\n")
        finally:
            shutil.rmtree(tmp)

    def test_output_accumulator_reset_clears_file(self) -> None:
        tmp = Path(mkdtemp())
        try:
            path = tmp / "test-reset.output.txt"
            acc = _OutputAccumulator(path)
            acc.append("data\n")
            self.assertTrue(path.exists())
            acc.reset()
            self.assertFalse(path.exists())
            self.assertEqual(acc.text, "")
        finally:
            shutil.rmtree(tmp)

    def test_wezterm_host_handles_scrollback_shift(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        get_text_responses = iter([
            "line1\nline2\nline3\n",
            "line2\nline3\nline4\nline5\n",
            "line4\nline5\nline6\n",
        ])

        def runner(argv: list[str], input: str | None = None, **_: object) -> Result:  # noqa: ARG001
            if argv[2] == "get-text":
                return Result(next(get_text_responses))
            if argv[2] == "list":
                return Result(
                    json.dumps(
                        [{"pane_id": 99, "tab_id": 1, "window_id": 1,
                          "workspace": "agp-test", "window_title": "AGP:agt_shift",
                          "tab_title": "AGP:agt_shift", "cwd": "/tmp"}]
                    )
                )
            raise AssertionError(f"unexpected: {argv}")

        tmp = Path(mkdtemp())
        try:
            host = WezTermHost(workspace="agp-test", runner=runner, checkpoint_dir=tmp)
            session = host.get_or_create_session(agent_id="agt_shift")
            cursor = host.create_cursor(session)

            r1 = host.read_output(session, cursor)
            self.assertTrue(r1.changed)
            self.assertEqual(r1.text, "line4\nline5\n")

            r2 = host.read_output(session, r1.cursor)
            self.assertTrue(r2.changed)
            self.assertEqual(r2.text, "line6\n")
            self.assertIn("line4", r2.full_text)
            self.assertIn("line6", r2.full_text)
        finally:
            shutil.rmtree(tmp)

    def test_wezterm_host_accumulator_captures_full_transcript(self) -> None:
        class Result:
            def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
                self.stdout = stdout
                self.stderr = stderr
                self.returncode = returncode

        get_text_responses = iter([
            "a\nb\n",
            "a\nb\nc\nd\n",
            "c\nd\ne\nf\n",
        ])

        def runner(argv: list[str], input: str | None = None, **_: object) -> Result:  # noqa: ARG001
            if argv[2] == "get-text":
                return Result(next(get_text_responses))
            if argv[2] == "list":
                return Result(
                    json.dumps(
                        [{"pane_id": 88, "tab_id": 1, "window_id": 1,
                          "workspace": "agp-test", "window_title": "AGP:agt_acc",
                          "tab_title": "AGP:agt_acc", "cwd": "/tmp"}]
                    )
                )
            raise AssertionError(f"unexpected: {argv}")

        tmp = Path(mkdtemp())
        try:
            host = WezTermHost(workspace="agp-test", runner=runner, checkpoint_dir=tmp)
            session = host.get_or_create_session(agent_id="agt_acc")
            cursor = host.create_cursor(session)

            r1 = host.read_output(session, cursor)
            r2 = host.read_output(session, r1.cursor)
            self.assertIn("c\nd\n", r1.full_text)
            self.assertIn("e\nf\n", r2.full_text)
            self.assertIn("c\nd\n", r2.full_text)
        finally:
            shutil.rmtree(tmp)

    # ── Gap-closure tests: CodexAdapter hardening ────────────────────

    def test_codex_adapter_malformed_payload_triggers_recovery(self) -> None:
        class CodexHost(InProcessTerminalHost):
            def send_text(self, session, text: str, *, enter: bool = True) -> None:
                super().send_text(session, text, enter=enter)
                if text.startswith("AGP_RUN_BEGIN run_malformed"):
                    self._history.setdefault(session.session_id, []).append(
                        "AGP_RUN_RESULT run_malformed {not valid json\n"
                    )

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_mal"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(max_polls=2, poll_interval_seconds=0.0)
        host = CodexHost()
        session = host.get_or_create_session(agent_id="agt_mal")
        claimed = {
            "agent_id": "agt_mal",
            "job": {"job_id": "job_mal"},
            "run": {"run_id": "run_malformed"},
            "message": {"text": "malformed work"},
        }
        with self.assertRaises(RecoverableExecutionError):
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())

    def test_codex_adapter_idle_timeout_triggers_recovery(self) -> None:
        host = InProcessTerminalHost()
        session = host.get_or_create_session(agent_id="agt_idle")

        class SupervisorStub:
            def __init__(self) -> None:
                self.client = type("Client", (), {"identity": type("Identity", (), {"runtime_id": "rtm_idle"})()})()

            def check_interrupt(self, claimed: dict[str, object]) -> None:  # noqa: ARG002
                return None

            def emit_progress(self, claimed: dict[str, object], *, message: str, details: dict | None = None) -> dict:  # noqa: ARG002
                return {"status": "ok"}

        adapter = CodexAdapter(max_polls=10, poll_interval_seconds=0.0, idle_timeout_polls=3)
        claimed = {
            "agent_id": "agt_idle",
            "job": {"job_id": "job_idle"},
            "run": {"run_id": "run_idle"},
            "message": {"text": "wedge work"},
        }
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.execute_run(host=host, session=session, claimed=claimed, supervisor=SupervisorStub())
        self.assertIn("idle", str(ctx.exception))

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

    # ── Gap-closure tests: ANSI stripping and Codex TUI cleaning ─────

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

    # ── Gap-closure tests: TUI mode CodexAdapter ─────────────────────

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
                        "Here is the result of your task.\n"
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

        adapter = CodexAdapter(tui_mode=True, cli_command="ncodex", idle_poll_seconds=0.0, idle_after=1)
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

        adapter = CodexAdapter(tui_mode=True, cli_command="ncodex", idle_poll_seconds=0.0, idle_after=1)
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

    # ── Gap-closure tests: CLI exit detection ────────────────────────

    def test_codex_adapter_tui_detects_shell_returned_during_bootstrap(self) -> None:
        class ShellReturnHost(InProcessTerminalHost):
            """Host where read_visible shows a shell prompt (CLI exited)."""
            def read_visible(self, session):
                return "\u276f some shell prompt\n"

        adapter = CodexAdapter(tui_mode=True, cli_command="ncodex", idle_poll_seconds=0.0)
        host = ShellReturnHost()
        session = host.get_or_create_session(agent_id="agt_exit_boot")
        with self.assertRaises(RecoverableExecutionError) as ctx:
            adapter.ensure_bootstrapped(host=host, session=session, claimed={})
        self.assertIn("exited back to shell", str(ctx.exception))

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

    # ── Gap-closure tests: cursor persistence ────────────────────────

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


if __name__ == "__main__":
    unittest.main()
