"""Core agent/job/orchestration and artifact flows."""

from tempfile import TemporaryDirectory

from agp.artifact_store import get_artifact_store
from agp.runtime import TerminalSession

from tests.mvp_flow.base import *


class MvpFlowCoreTest(MvpFlowTestBase):
    def _claim_single_run(self, *, agent_id: str, runtime_id: str, text: str = "work") -> dict:
        self.client.post("/runtimes/register", json={"runtime_id": runtime_id, "hostname": runtime_id})
        self.client.post("/agents/up", json={"agent_id": agent_id, "capabilities": ["python"]})
        send = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": agent_id},
                "message": {"text": text, "metadata": {}},
            },
            headers={"Idempotency-Key": f"{agent_id}-{runtime_id}-{text}"},
        )
        self.assertEqual(send.status_code, 200)
        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": runtime_id, "agent_id": agent_id},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]
        self.assertTrue(claim_data["claimed"])
        return claim_data

    def _assert_agent_and_runtime_idle(self, *, agent_id: str, runtime_id: str) -> None:
        agent = self.client.get(f"/agents/{agent_id}")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["data"]["status"], "idle")
        runtime = self.client.get(f"/runtimes/{runtime_id}")
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(runtime.json()["data"]["status"], "idle")
        self.assertEqual(runtime.json()["data"]["active_run_count"], 0)

    def _assert_agent_and_runtime_busy(self, *, agent_id: str, runtime_id: str, active_run_count: int) -> None:
        agent = self.client.get(f"/agents/{agent_id}")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["data"]["status"], "busy")
        runtime = self.client.get(f"/runtimes/{runtime_id}")
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(runtime.json()["data"]["status"], "busy")
        self.assertEqual(runtime.json()["data"]["active_run_count"], active_run_count)

    def _seed_active_run_and_lease(self, *, agent_id: str, runtime_id: str, suffix: str) -> None:
        now = utc_now()
        session = SessionLocal()
        try:
            message = Message(
                message_id=f"msg_{suffix}",
                target_type="agent",
                target_id=agent_id,
                text=f"extra-{suffix}",
                metadata_json={},
                conversation_id=f"conv_{suffix}",
                created_at=now,
            )
            job = Job(
                job_id=f"job_{suffix}",
                message_id=message.message_id,
                target_agent_id=agent_id,
                target_queue=f"agent:{agent_id}",
                status="running",
                retry_count=0,
                max_retries=3,
                latest_run_id=f"run_{suffix}",
                conversation_id=message.conversation_id,
                created_at=now,
                updated_at=now,
            )
            run = Run(
                run_id=f"run_{suffix}",
                job_id=job.job_id,
                agent_id=agent_id,
                runtime_id=runtime_id,
                attempt=1,
                status="running",
                started_at=now,
                created_at=now,
            )
            lease = Lease(
                lease_id=f"lease_{suffix}",
                run_id=run.run_id,
                agent_id=agent_id,
                runtime_id=runtime_id,
                fencing_token=99,
                status="active",
                expires_at=now + timedelta(minutes=5),
                created_at=now,
            )
            session.add_all([message, job, run, lease])
            session.commit()
        finally:
            session.close()

    def _seed_active_run_only(self, *, agent_id: str, runtime_id: str, suffix: str) -> None:
        now = utc_now()
        session = SessionLocal()
        try:
            message = Message(
                message_id=f"msg_{suffix}",
                target_type="agent",
                target_id=agent_id,
                text=f"extra-{suffix}",
                metadata_json={},
                conversation_id=f"conv_{suffix}",
                created_at=now,
            )
            job = Job(
                job_id=f"job_{suffix}",
                message_id=message.message_id,
                target_agent_id=agent_id,
                target_queue=f"agent:{agent_id}",
                status="running",
                retry_count=0,
                max_retries=3,
                latest_run_id=f"run_{suffix}",
                conversation_id=message.conversation_id,
                created_at=now,
                updated_at=now,
            )
            run = Run(
                run_id=f"run_{suffix}",
                job_id=job.job_id,
                agent_id=agent_id,
                runtime_id=runtime_id,
                attempt=1,
                status="running",
                started_at=now,
                created_at=now,
            )
            session.add_all([message, job, run])
            session.commit()
        finally:
            session.close()

    def _seed_active_lease_only(self, *, runtime_id: str, suffix: str) -> None:
        now = utc_now()
        session = SessionLocal()
        try:
            message = Message(
                message_id=f"msg_{suffix}",
                target_type="agent",
                target_id=f"agt_{suffix}",
                text=f"extra-{suffix}",
                metadata_json={},
                conversation_id=f"conv_{suffix}",
                created_at=now,
            )
            job = Job(
                job_id=f"job_{suffix}",
                message_id=message.message_id,
                target_agent_id=None,
                target_queue=f"agent:agt_{suffix}",
                status="completed",
                retry_count=0,
                max_retries=3,
                latest_run_id=f"run_{suffix}",
                conversation_id=message.conversation_id,
                created_at=now,
                updated_at=now,
            )
            run = Run(
                run_id=f"run_{suffix}",
                job_id=job.job_id,
                agent_id=f"agt_{suffix}",
                runtime_id=runtime_id,
                attempt=1,
                status="completed",
                started_at=now,
                finished_at=now,
                created_at=now,
            )
            lease = Lease(
                lease_id=f"lease_{suffix}",
                run_id=run.run_id,
                agent_id=f"agt_{suffix}",
                runtime_id=runtime_id,
                fencing_token=99,
                status="active",
                expires_at=now + timedelta(minutes=5),
                created_at=now,
            )
            session.add_all([message, job, run, lease])
            session.commit()
        finally:
            session.close()

    def test_complete_run_returns_agent_and_runtime_to_idle(self) -> None:
        claim = self._claim_single_run(agent_id="agt_complete_idle", runtime_id="rtm_complete_idle", text="complete")
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_complete_idle",
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
        self._assert_agent_and_runtime_idle(agent_id="agt_complete_idle", runtime_id="rtm_complete_idle")

    def test_fail_run_returns_agent_and_runtime_to_idle(self) -> None:
        claim = self._claim_single_run(agent_id="agt_fail_idle", runtime_id="rtm_fail_idle", text="fail")
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        fail = self.client.post(
            f"/runs/{run_id}/fail",
            json={
                "runtime_id": "rtm_fail_idle",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "error": "boom",
                "artifacts": self._materialize_terminal_artifacts(
                    {
                        "prompt.txt": "prompt",
                        "transcript.txt": "transcript_log",
                        "exec.txt": "exec_log",
                        "result.txt": "result",
                        "failure.txt": "failure_evidence",
                    }
                ),
                "summary": {"ok": False},
            },
        )
        self.assertEqual(fail.status_code, 200)
        self._assert_agent_and_runtime_idle(agent_id="agt_fail_idle", runtime_id="rtm_fail_idle")

    def test_cancel_run_returns_agent_and_runtime_to_idle(self) -> None:
        claim = self._claim_single_run(agent_id="agt_cancel_idle", runtime_id="rtm_cancel_idle", text="cancel")
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        cancel = self.client.post(
            f"/runs/{run_id}/cancel",
            json={
                "runtime_id": "rtm_cancel_idle",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "reason": "test",
            },
        )
        self.assertEqual(cancel.status_code, 200)
        self._assert_agent_and_runtime_idle(agent_id="agt_cancel_idle", runtime_id="rtm_cancel_idle")

    def test_complete_run_stays_busy_when_other_active_work_exists(self) -> None:
        claim = self._claim_single_run(agent_id="agt_complete_busy", runtime_id="rtm_complete_busy", text="first")
        self._seed_active_run_and_lease(
            agent_id="agt_complete_busy",
            runtime_id="rtm_complete_busy",
            suffix="complete_busy_extra",
        )
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_complete_busy",
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
        self._assert_agent_and_runtime_busy(
            agent_id="agt_complete_busy",
            runtime_id="rtm_complete_busy",
            active_run_count=1,
        )

    def test_complete_run_keeps_agent_busy_when_other_active_run_exists(self) -> None:
        claim = self._claim_single_run(agent_id="agt_run_only_busy", runtime_id="rtm_run_only_busy", text="first")
        self._seed_active_run_only(
            agent_id="agt_run_only_busy",
            runtime_id="rtm_run_only_busy",
            suffix="run_only_busy_extra",
        )
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_run_only_busy",
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

        agent = self.client.get("/agents/agt_run_only_busy")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["data"]["status"], "busy")
        runtime = self.client.get("/runtimes/rtm_run_only_busy")
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(runtime.json()["data"]["status"], "idle")
        self.assertEqual(runtime.json()["data"]["active_run_count"], 0)

    def test_complete_run_keeps_runtime_busy_when_other_active_lease_exists(self) -> None:
        claim = self._claim_single_run(agent_id="agt_lease_only_busy", runtime_id="rtm_lease_only_busy", text="first")
        self._seed_active_lease_only(
            runtime_id="rtm_lease_only_busy",
            suffix="lease_only_busy_extra",
        )
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_lease_only_busy",
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

        agent = self.client.get("/agents/agt_lease_only_busy")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["data"]["status"], "idle")
        runtime = self.client.get("/runtimes/rtm_lease_only_busy")
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(runtime.json()["data"]["status"], "busy")
        self.assertEqual(runtime.json()["data"]["active_run_count"], 1)

    def test_fail_run_preserves_draining_status(self) -> None:
        claim = self._claim_single_run(agent_id="agt_fail_drain", runtime_id="rtm_fail_drain", text="fail-drain")
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        drain = self.client.post("/agents/agt_fail_drain/down", json={"mode": "drain"})
        self.assertEqual(drain.status_code, 200)

        fail = self.client.post(
            f"/runs/{run_id}/fail",
            json={
                "runtime_id": "rtm_fail_drain",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "error": "boom",
                "artifacts": self._materialize_terminal_artifacts(
                    {
                        "prompt.txt": "prompt",
                        "transcript.txt": "transcript_log",
                        "exec.txt": "exec_log",
                        "failure.txt": "failure_evidence",
                    }
                ),
                "summary": {"ok": False},
            },
        )
        self.assertEqual(fail.status_code, 200)
        agent = self.client.get("/agents/agt_fail_drain")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["data"]["status"], "draining")

    def test_cancel_run_preserves_draining_status(self) -> None:
        claim = self._claim_single_run(agent_id="agt_cancel_drain", runtime_id="rtm_cancel_drain", text="cancel-drain")
        run_id = claim["run"]["run_id"]
        lease_id = claim["lease"]["lease_id"]
        fencing_token = claim["lease"]["fencing_token"]

        drain = self.client.post("/agents/agt_cancel_drain/down", json={"mode": "drain"})
        self.assertEqual(drain.status_code, 200)

        cancel = self.client.post(
            f"/runs/{run_id}/cancel",
            json={
                "runtime_id": "rtm_cancel_drain",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "reason": "test",
            },
        )
        self.assertEqual(cancel.status_code, 200)
        agent = self.client.get("/agents/agt_cancel_drain")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["data"]["status"], "draining")

    def test_runtime_worker_stops_heartbeats_when_tui_dies(self) -> None:
        class TuiAwareHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self._foreground_checks = 0

            def is_foreground_tui(self, session) -> bool:  # noqa: ARG002
                self._foreground_checks += 1
                return False

        class WaitingAdapter(DefaultAgentAdapter):
            @property
            def kind(self) -> str:
                return "waiting"

            def execute_run(self, *, host, session, claimed, supervisor):  # noqa: ARG002
                startup_settled = session.metadata.get("startup_settled_event")
                assert startup_settled is not None
                startup_settled.set()
                while True:
                    sleep(0.005)
                    supervisor.check_interrupt(claimed)

        class FakeRuntimeClient:
            def __init__(self) -> None:
                self._log_fn = None
                self.identity = type(
                    "Identity",
                    (),
                    {"runtime_id": "rtm_tui_dead", "server_url": "http://example.invalid", "token": None, "metadata": {}},
                )()
                self.heartbeat_calls: list[dict[str, object]] = []
                self.progress_calls: list[dict[str, object]] = []
                self.fail_calls: list[dict[str, object]] = []

            def claim(self, *, agent_id, capability, lease_ttl_seconds):  # noqa: ARG002
                return {
                    "claimed": True,
                    "agent_id": agent_id,
                    "job": {"job_id": "job_tui_dead", "status": "running"},
                    "run": {"run_id": "run_tui_dead"},
                    "lease": {"lease_id": "lease_tui_dead", "fencing_token": 7},
                    "message": {"text": "wait forever", "metadata": {}},
                }

            def register(self):
                return {"status": "ok"}

            def heartbeat(self, **kwargs):
                self.heartbeat_calls.append(kwargs)
                return {"interrupt_requested": False}

            def progress(self, **kwargs):
                self.progress_calls.append(kwargs)
                return {"status": "ok"}

            def get_job(self, job_id):  # noqa: ARG002
                return {"status": "running"}

            def fail(self, **kwargs):
                self.fail_calls.append(kwargs)
                return {"job_status": "failed"}

        class FakeHeartbeatResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": {"interrupt_requested": False}}

        heartbeat_posts: list[dict[str, object]] = []

        class FakeHeartbeatHttpClient:
            def __init__(self, *args, **kwargs):  # noqa: ARG002
                return None

            def post(self, url: str, json: dict[str, object]):
                heartbeat_posts.append({"url": url, "json": json})
                return FakeHeartbeatResponse()

            def close(self) -> None:
                return None

        host = TuiAwareHost()
        client = FakeRuntimeClient()
        worker = RuntimeSupervisor(
            client,
            host=host,
            adapter=WaitingAdapter(),
            artifact_root=".agp-artifacts-tests",
        )

        with patch("httpx.Client", FakeHeartbeatHttpClient):
            payload = worker.run_once(
                agent_id="agt_tui_dead",
                heartbeat_interval_seconds=0.01,
                lease_ttl_seconds=30,
            )

        self.assertTrue(payload["claimed"])
        self.assertEqual(payload["result"]["job_status"], "failed")
        self.assertEqual(host._foreground_checks, 1)
        self.assertEqual(len(client.heartbeat_calls), 1)
        self.assertEqual(len(heartbeat_posts), 2)
        self.assertEqual(len(client.fail_calls), 1)

    def test_runtime_worker_clears_startup_settled_before_recovery_retry(self) -> None:
        from agp.runtime import ArtifactPayload, ExecutionResult, PaneDied

        class RecoveringHost(InProcessTerminalHost):
            def __init__(self) -> None:
                super().__init__()
                self._session_serial = 0

            def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None):
                self._session_serial += 1
                session = super().get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
                session = type(session)(
                    session_id=f"inproc-{agent_id}-{self._session_serial}",
                    agent_id=session.agent_id,
                    workspace_ref=session.workspace_ref,
                    metadata=dict(session.metadata),
                )
                self._sessions[agent_id] = session
                self._history[session.session_id] = []
                return session

        class RecoveryAdapter(DefaultAgentAdapter):
            def __init__(self) -> None:
                super().__init__()
                self.execute_calls = 0
                self.ensure_bootstrapped_calls: list[tuple[str, bool]] = []

            @property
            def kind(self) -> str:
                return "recovery"

            def ensure_bootstrapped(self, *, host, session, claimed):  # noqa: ARG002
                event = session.metadata.get("startup_settled_event")
                self.ensure_bootstrapped_calls.append((session.session_id, bool(event and event.is_set())))

            def execute_run(self, *, host, session, claimed, supervisor):  # noqa: ARG002
                self.execute_calls += 1
                event = session.metadata.get("startup_settled_event")
                assert event is not None
                if self.execute_calls == 1:
                    assert not event.is_set()
                    event.set()
                    raise PaneDied("codex cli exited during execution")
                assert not event.is_set()
                event.set()
                return ExecutionResult(
                    artifacts=[ArtifactPayload(role="result", name="result.txt", content="ok")],
                    summary={"status": "completed"},
                )

        class FakeRuntimeClient:
            def __init__(self) -> None:
                self._log_fn = None
                self.identity = type(
                    "Identity",
                    (),
                    {"runtime_id": "rtm_recovery", "server_url": "http://example.invalid", "token": None, "metadata": {}},
                )()
                self.heartbeat_calls: list[dict[str, object]] = []
                self.recovering_calls: list[dict[str, object]] = []
                self.resumed_calls: list[dict[str, object]] = []
                self.complete_calls: list[dict[str, object]] = []

            def claim(self, *, agent_id, capability, lease_ttl_seconds):  # noqa: ARG002
                return {
                    "claimed": True,
                    "agent_id": agent_id,
                    "job": {"job_id": "job_recovery", "status": "running"},
                    "run": {"run_id": "run_recovery"},
                    "lease": {"lease_id": "lease_recovery", "fencing_token": 9},
                    "message": {"text": "recover me", "metadata": {}},
                }

            def register(self):
                return {"status": "ok"}

            def heartbeat(self, **kwargs):
                self.heartbeat_calls.append(kwargs)
                return {"interrupt_requested": False}

            def progress(self, **kwargs):  # noqa: ARG002
                return {"status": "ok"}

            def recovering(self, **kwargs):
                self.recovering_calls.append(kwargs)
                return {"status": "ok"}

            def resumed(self, **kwargs):
                self.resumed_calls.append(kwargs)
                return {"status": "ok"}

            def complete(self, **kwargs):
                self.complete_calls.append(kwargs)
                return {"job_status": "completed"}

        class FakeHeartbeatResponse:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": {"interrupt_requested": False}}

        class FakeHeartbeatHttpClient:
            def __init__(self, *args, **kwargs):  # noqa: ARG002
                return None

            def post(self, url: str, json: dict[str, object]):  # noqa: ARG002
                return FakeHeartbeatResponse()

            def close(self) -> None:
                return None

        host = RecoveringHost()
        adapter = RecoveryAdapter()
        client = FakeRuntimeClient()
        worker = RuntimeSupervisor(
            client,
            host=host,
            adapter=adapter,
            artifact_root=".agp-artifacts-tests",
        )

        with patch("httpx.Client", FakeHeartbeatHttpClient):
            payload = worker.run_once(
                agent_id="agt_recovery",
                heartbeat_interval_seconds=10.0,
                lease_ttl_seconds=30,
            )

        self.assertTrue(payload["claimed"])
        self.assertEqual(payload["result"]["job_status"], "completed")
        self.assertEqual(adapter.execute_calls, 2)
        self.assertEqual(len(client.recovering_calls), 1)
        self.assertEqual(len(client.resumed_calls), 1)
        self.assertEqual(len(client.complete_calls), 1)
        self.assertEqual(
            adapter.ensure_bootstrapped_calls,
            [
                ("inproc-agt_recovery-1", False),
                ("inproc-agt_recovery-2", False),
            ],
        )

    def test_send_with_attachments_and_claim_returns_them(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_attach", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_attach", "hostname": "localhost"})

        sent = self.agp.send(
            target_type="agent",
            target_id="agt_attach",
            text="review these files",
            attachments=[
                {"name": "diff.patch", "role": "diff", "content": "diff --git a/x b/x\n"},
                {"name": "spec.md", "role": "spec", "content": "# Spec\n"},
            ],
            idempotency_key="attach-send-1",
        )

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_attach", "agent_id": "agt_attach"},
        )
        self.assertEqual(claim.status_code, 200)
        payload = claim.json()["data"]
        self.assertEqual(payload["job"]["job_id"], sent["job_id"])
        self.assertEqual(len(payload["job_attachments"]), 2)
        self.assertEqual(
            [(item["name"], item["role"]) for item in payload["job_attachments"]],
            [("diff.patch", "diff"), ("spec.md", "spec")],
        )
        self.assertTrue(all(item["artifact_id"] for item in payload["job_attachments"]))
        self.assertTrue(all(item["storage_ref"] for item in payload["job_attachments"]))

    def test_send_without_attachments_claim_returns_empty_attachments(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_attach_empty", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_attach_empty", "hostname": "localhost"})

        sent = self.agp.send(
            target_type="agent",
            target_id="agt_attach_empty",
            text="no files",
            idempotency_key="attach-send-empty-1",
        )

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_attach_empty", "agent_id": "agt_attach_empty"},
        )
        self.assertEqual(claim.status_code, 200)
        payload = claim.json()["data"]
        self.assertEqual(payload["job"]["job_id"], sent["job_id"])
        self.assertEqual(payload["job_attachments"], [])

    def test_send_with_conversation_id_persists_to_message_and_job(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_conv", "capabilities": ["python"]})

        sent = self.agp.send(
            target_type="agent",
            target_id="agt_conv",
            text="first turn",
            conversation_id="conv_123",
            idempotency_key="conv-send-1",
        )

        job = self.agp.get_job(sent["job_id"])
        self.assertEqual(job["conversation_id"], "conv_123")

        with SessionLocal() as db:
            message = db.get(Message, job["message_id"])
            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(message.conversation_id, "conv_123")
            self.assertIsNone(message.reply_to_message_id)

    def test_claim_returns_conversation_context_with_prior_messages(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_conv_claim", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_conv_claim", "hostname": "localhost"})

        first = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_conv_claim"},
                "message": {
                    "text": "hello",
                    "metadata": {"role": "user"},
                    "conversation_id": "conv_claim",
                },
            },
            headers={"Idempotency-Key": "conv-claim-1"},
        ).json()["data"]

        with SessionLocal() as db:
            first_job = db.get(Job, first["job_id"])
            assert first_job is not None
            first_job.status = "cancelled"
            first_message = db.get(Message, first_job.message_id)
            assert first_message is not None
            second = Message(
                message_id="msg_prior_agent",
                target_type="agent",
                target_id="agt_conv_claim",
                text="previous assistant reply",
                metadata_json={"role": "assistant"},
                conversation_id="conv_claim",
                reply_to_message_id=first_message.message_id,
            )
            db.add(second)
            db.commit()

        third = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_conv_claim"},
                "message": {
                    "text": "follow-up",
                    "metadata": {},
                    "conversation_id": "conv_claim",
                    "reply_to_message_id": first["message_id"],
                },
            },
            headers={"Idempotency-Key": "conv-claim-2"},
        ).json()["data"]

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_conv_claim", "agent_id": "agt_conv_claim"},
        )
        self.assertEqual(claim.status_code, 200)
        payload = claim.json()["data"]
        self.assertEqual(payload["job"]["job_id"], third["job_id"])
        self.assertEqual(payload["job"]["conversation_id"], "conv_claim")
        self.assertEqual([item["text"] for item in payload["conversation_context"]], ["hello", "previous assistant reply", "follow-up"])
        self.assertEqual([item["role"] for item in payload["conversation_context"]], ["user", "assistant", "user"])

    def test_reply_cli_uses_source_job_conversation_and_message(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_reply", "capabilities": ["python"]})
        first = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_reply"},
                "message": {"text": "seed", "metadata": {}, "conversation_id": "conv_reply"},
            },
            headers={"Idempotency-Key": "cli-reply-seed"},
        ).json()["data"]

        result = self._cli_invoke(["reply", first["job_id"], "second turn", "--detach"])
        self.assertEqual(result.exit_code, 0)

        jobs = self.agp.list_jobs(target_agent_id="agt_reply")["items"]
        reply_job = next(item for item in jobs if item["job_id"] != first["job_id"])
        self.assertEqual(reply_job["conversation_id"], "conv_reply")

        with SessionLocal() as db:
            reply_message = db.get(Message, reply_job["message_id"])
            self.assertIsNotNone(reply_message)
            assert reply_message is not None
            self.assertEqual(reply_message.conversation_id, "conv_reply")
            self.assertEqual(reply_message.reply_to_message_id, first["message_id"])

    def test_send_without_conversation_id_auto_generates_one(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_conv_legacy", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_conv_legacy", "hostname": "localhost"})

        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_conv_legacy"},
                "message": {"text": "legacy convo", "metadata": {}},
            },
            headers={"Idempotency-Key": "conv-legacy-1"},
        )
        self.assertEqual(sent.status_code, 200)
        payload = sent.json()["data"]

        job = self.agp.get_job(payload["job_id"])
        # Server auto-generates a conversation_id for new conversations
        self.assertIsNotNone(job["conversation_id"])

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_conv_legacy", "agent_id": "agt_conv_legacy"},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]
        self.assertIsNotNone(claim_data["job"]["conversation_id"])
        # Single-message conversation has context with just this one message
        self.assertEqual(len(claim_data["conversation_context"]), 1)

    def test_send_with_timeout_seconds_sets_deadline(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_timeout_hint", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_timeout_hint", "hostname": "localhost"})

        before = utc_now()
        sent = self.agp.send(
            target_type="agent",
            target_id="agt_timeout_hint",
            text="finish soon",
            timeout_seconds=30,
            idempotency_key="timeout-send-1",
        )
        after = utc_now()

        job = self.agp.get_job(sent["job_id"])
        self.assertEqual(job["timeout_seconds"], 30)
        self.assertIsNotNone(job["deadline_at"])
        deadline = job["deadline_at"]
        self.assertGreaterEqual(deadline, before + timedelta(seconds=30))
        self.assertLessEqual(deadline, after + timedelta(seconds=30))

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_timeout_hint", "agent_id": "agt_timeout_hint"},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]
        self.assertEqual(claim_data["job"]["timeout_seconds"], 30)
        self.assertIsNotNone(claim_data["job"]["deadline_at"])

    def test_complete_after_deadline_fails_with_timeout(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_timeout_fail", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_timeout_fail", "hostname": "localhost"})

        send = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_timeout_fail"},
                "message": {"text": "will expire", "metadata": {}, "timeout_seconds": 1},
            },
        )
        self.assertEqual(send.status_code, 200)
        job_id = send.json()["data"]["job_id"]

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_timeout_fail", "agent_id": "agt_timeout_fail"},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]

        with SessionLocal() as db:
            job = db.get(Job, job_id)
            assert job is not None
            job.deadline_at = utc_now() - timedelta(seconds=1)
            db.commit()

        complete = self.client.post(
            f"/runs/{claim_data['run']['run_id']}/complete",
            json={
                "runtime_id": "rtm_timeout_fail",
                "lease_id": claim_data["lease"]["lease_id"],
                "fencing_token": claim_data["lease"]["fencing_token"],
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
        self.assertEqual(complete.status_code, 409)
        self.assertEqual(complete.json()["error"]["message"], "job deadline exceeded")

        job = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["data"]["status"], "failed")

    def test_job_without_timeout_remains_backward_compatible(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_no_timeout", "capabilities": ["python"]})
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_no_timeout", "hostname": "localhost"},
        )

        send = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_no_timeout"},
                "message": {"text": "legacy no timeout", "metadata": {}},
            },
        )
        self.assertEqual(send.status_code, 200)
        job_id = send.json()["data"]["job_id"]

        job = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertIsNone(job.json()["data"]["timeout_seconds"])
        self.assertIsNone(job.json()["data"]["deadline_at"])

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_no_timeout", "agent_id": "agt_no_timeout"},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]
        self.assertIsNone(claim_data["job"]["timeout_seconds"])
        self.assertIsNone(claim_data["job"]["deadline_at"])

    def test_job_with_output_contract_and_valid_json_result_completes(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_contract", "capabilities": ["python"]})
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_contract", "hostname": "localhost"},
        )

        send = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_contract"},
                "message": {
                    "text": "return structured output",
                    "metadata": {},
                    "output_contract": {
                        "format": "json",
                        "json_schema": {
                            "type": "object",
                            "required": ["status"],
                            "properties": {"status": {"type": "string"}},
                        },
                    },
                },
            },
        )
        self.assertEqual(send.status_code, 200)
        job_id = send.json()["data"]["job_id"]

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_contract", "agent_id": "agt_contract"},
        )
        self.assertEqual(claim.status_code, 200)
        claim_data = claim.json()["data"]
        self.assertEqual(
            claim_data["job"]["output_contract_json"],
            {
                "format": "json",
                "json_schema": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string"}},
                },
            },
        )

        run_id = claim_data["run"]["run_id"]
        lease_id = claim_data["lease"]["lease_id"]
        fencing_token = claim_data["lease"]["fencing_token"]
        complete = self.client.post(
            f"/runs/{run_id}/complete",
            json={
                "runtime_id": "rtm_contract",
                "lease_id": lease_id,
                "fencing_token": fencing_token,
                "artifacts": self._materialize_terminal_artifacts(
                    {
                        "prompt.txt": "prompt",
                        "transcript.txt": "transcript_log",
                        "exec.txt": "exec_log",
                        "result.json": "result",
                    },
                    contents={"result.json": '{"status":"ok"}\n'},
                ),
                "summary": {"ok": True},
            },
        )
        self.assertEqual(complete.status_code, 200)

        job = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["data"]["status"], "completed")
        self.assertEqual(
            job.json()["data"]["output_contract_json"],
            claim_data["job"]["output_contract_json"],
        )

    def test_job_with_output_contract_and_invalid_json_result_fails_completion(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_contract_bad", "capabilities": ["python"]})
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_contract_bad", "hostname": "localhost"},
        )

        send = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_contract_bad"},
                "message": {
                    "text": "return structured output",
                    "metadata": {},
                    "output_contract": {"format": "json", "json_schema": {"type": "object"}},
                },
            },
        )
        self.assertEqual(send.status_code, 200)
        job_id = send.json()["data"]["job_id"]

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_contract_bad", "agent_id": "agt_contract_bad"},
        ).json()["data"]

        complete = self.client.post(
            f"/runs/{claim['run']['run_id']}/complete",
            json={
                "runtime_id": "rtm_contract_bad",
                "lease_id": claim["lease"]["lease_id"],
                "fencing_token": claim["lease"]["fencing_token"],
                "artifacts": self._materialize_terminal_artifacts(
                    {
                        "prompt.txt": "prompt",
                        "transcript.txt": "transcript_log",
                        "exec.txt": "exec_log",
                        "result.json": "result",
                    },
                    contents={"result.json": '{"status": "ok"\n'},
                ),
                "summary": {"ok": True},
            },
        )
        self.assertEqual(complete.status_code, 422)
        err = complete.json()["error"]
        self.assertEqual(err["code"], "output_contract_validation_failed")
        self.assertIn("result artifact is not valid JSON", err["message"])

        job = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["data"]["status"], "failed")

        trace = self.client.get(f"/observability/jobs/{job_id}/trace")
        self.assertEqual(trace.status_code, 200)
        self.assertEqual(trace.json()["data"]["runs"][-1]["status"], "failed")

        failure_artifacts = self.client.get(f"/jobs/{job_id}/artifacts?role=failure_evidence")
        self.assertEqual(failure_artifacts.status_code, 200)
        items = failure_artifacts.json()["data"]["items"]
        self.assertTrue(items)

        failure_content = self.client.get(f"/artifacts/{items[0]['artifact_id']}/content")
        self.assertEqual(failure_content.status_code, 200)
        self.assertIn(
            "result artifact is not valid JSON",
            failure_content.json()["data"]["content"],
        )

    def test_inline_send_with_output_contract_skips_contract_validation(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_inline_contract", "capabilities": ["python"]})

        response = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_inline_contract"},
                "message": {
                    "text": "inline contract please",
                    "metadata": {},
                    "output_contract": {
                        "format": "json",
                        "json_schema": {
                            "type": "object",
                            "required": ["status"],
                            "properties": {"status": {"type": "string"}},
                        },
                    },
                },
                "detach_policy": {"mode": "inline"},
            },
            headers={"Idempotency-Key": "inline-contract-flow-1"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["kind"], "inline_result")
        self.assertEqual(payload["status"], "completed")

        job = self.client.get(f"/jobs/{payload['job_id']}").json()["data"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(
            job["output_contract_json"],
            {
                "format": "json",
                "json_schema": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string"}},
                },
            },
        )

    def test_job_without_output_contract_remains_backward_compatible(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_no_contract", "capabilities": ["python"]})
        self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_no_contract", "hostname": "localhost"},
        )

        send = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_no_contract"},
                "message": {"text": "legacy flow", "metadata": {}},
            },
        )
        self.assertEqual(send.status_code, 200)
        job_id = send.json()["data"]["job_id"]

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_no_contract", "agent_id": "agt_no_contract"},
        ).json()["data"]
        self.assertIsNone(claim["job"]["output_contract_json"])

        complete = self.client.post(
            f"/runs/{claim['run']['run_id']}/complete",
            json={
                "runtime_id": "rtm_no_contract",
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

        job = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["data"]["status"], "completed")
        self.assertIsNone(job.json()["data"]["output_contract_json"])

    def test_agent_targeted_job_completes(self) -> None:
        agent = self.client.post("/agents/up", json={"agent_id": "agt_one", "capabilities": ["python"]})
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
        self.client.post("/agents/up", json={"agent_id": "agt_inline", "capabilities": ["python"]})
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
        self.client.post("/agents/up", json={"agent_id": "agt_page", "capabilities": ["python"]})
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
        # agent_up auto-creates an internal runtime (1:1 binding)
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_filter", "capabilities": ["python"]},
        )
        agents = self.client.get("/agents", params={"capability": "python", "status": "idle"})
        self.assertEqual(agents.status_code, 200)
        self.assertEqual(len(agents.json()["data"]["items"]), 1)

        runtimes = self.client.get("/runtimes", params={"status": "idle", "health_status": "healthy"})
        self.assertEqual(runtimes.status_code, 200)
        self.assertGreaterEqual(len(runtimes.json()["data"]["items"]), 1)

    def test_agent_endpoints_return_metadata_json(self) -> None:
        up = self.client.post(
            "/agents/up",
            json={
                "agent_id": "agt_meta",
                "capabilities": ["python"],
                "metadata": {"team": "control-plane"},
            },
        )
        self.assertEqual(up.status_code, 200)
        payload = up.json()["data"]
        self.assertEqual(payload["metadata_json"], {"team": "control-plane"})
        self.assertNotIn("metadata", payload)

        detail = self.client.get("/agents/agt_meta")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["metadata_json"], {"team": "control-plane"})

    def test_ops_runtime_drain_and_restart_endpoints_update_runtime_state(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_ops", "hostname": "localhost"})

        drain = self.client.post("/ops/runtimes/rtm_ops/drain")
        self.assertEqual(drain.status_code, 200)
        self.assertEqual(drain.json()["data"], {"runtime_id": "rtm_ops", "status": "draining"})

        runtime = self.client.get("/ops/runtimes/rtm_ops")
        self.assertEqual(runtime.status_code, 200)
        runtime_data = runtime.json()["data"]
        self.assertEqual(runtime_data["status"], "draining")
        self.assertEqual(runtime_data["health_status"], "draining")

        restart = self.client.post("/ops/runtimes/rtm_ops/restart")
        self.assertEqual(restart.status_code, 200)
        self.assertEqual(restart.json()["data"], {"runtime_id": "rtm_ops", "status": "idle"})

        runtime = self.client.get("/ops/runtimes/rtm_ops")
        self.assertEqual(runtime.status_code, 200)
        runtime_data = runtime.json()["data"]
        self.assertEqual(runtime_data["status"], "idle")
        self.assertEqual(runtime_data["health_status"], "healthy")

        session = SessionLocal()
        try:
            event_types = [
                row[0]
                for row in session.execute(
                    select(Event.event_type)
                    .where(Event.runtime_id == "rtm_ops")
                    .where(Event.event_type.in_(("runtime.draining", "runtime.restarted")))
                    .order_by(Event.event_seq.asc())
                ).all()
            ]
        finally:
            session.close()
        self.assertEqual(event_types, ["runtime.draining", "runtime.restarted"])

    def test_ops_runtime_restart_rejects_runtime_with_active_lease(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_ops_busy", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_ops_busy", "hostname": "localhost"})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_ops_busy"},
                "message": {"text": "keep runtime leased", "metadata": {}},
            },
            headers={"Idempotency-Key": "ops-restart-busy-1"},
        ).json()["data"]

        claim = self.client.post(
            "/runs/claim",
            json={"runtime_id": "rtm_ops_busy", "agent_id": "agt_ops_busy"},
        )
        self.assertEqual(claim.status_code, 200)
        self.assertTrue(claim.json()["data"]["claimed"])
        self.assertEqual(claim.json()["data"]["job"]["job_id"], sent["job_id"])

        restart = self.client.post("/ops/runtimes/rtm_ops_busy/restart")
        self.assertEqual(restart.status_code, 409)
        self.assertIn("active leases", restart.json()["error"]["message"])

        runtime = self.client.get("/ops/runtimes/rtm_ops_busy")
        self.assertEqual(runtime.status_code, 200)
        self.assertEqual(runtime.json()["data"]["status"], "busy")

    def test_runtime_re_register_preserves_draining_state(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_reup", "hostname": "localhost"})

        drain = self.client.post("/ops/runtimes/rtm_reup/drain")
        self.assertEqual(drain.status_code, 200)

        reup = self.client.post(
            "/runtimes/register",
            json={"runtime_id": "rtm_reup", "hostname": "localhost-2", "metadata": {"rev": "2"}},
        )
        self.assertEqual(reup.status_code, 200)
        payload = reup.json()["data"]
        self.assertEqual(payload["status"], "draining")
        self.assertEqual(payload["health_status"], "draining")

        runtime = self.client.get("/ops/runtimes/rtm_reup")
        self.assertEqual(runtime.status_code, 200)
        runtime_data = runtime.json()["data"]
        self.assertEqual(runtime_data["status"], "draining")
        self.assertEqual(runtime_data["health_status"], "draining")
        self.assertEqual(runtime_data["metadata_json"], {"rev": "2"})

    def test_artifacts_upload_endpoint_writes_content(self) -> None:
        upload = self.client.post(
            "/artifacts/upload",
            json={
                "namespace": "tests",
                "job_id": "job_upload",
                "name": "note.txt",
                "content": "hello upload\n",
                "role": "transcript_log",
                "content_type": "text/plain",
            },
        )
        self.assertEqual(upload.status_code, 200)
        payload = upload.json()["data"]
        self.assertEqual(payload["role"], "transcript_log")
        self.assertEqual(payload["content_type"], "text/plain")
        self.assertGreater(payload["size_bytes"], 0)
        self.assertTrue(payload["checksum"])

        store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
        self.assertEqual(store.read_text(storage_ref=payload["storage_ref"]), "hello upload\n")

    def test_artifacts_upload_endpoint_rejects_empty_content_type(self) -> None:
        upload = self.client.post(
            "/artifacts/upload",
            json={
                "namespace": "tests",
                "job_id": "job_upload",
                "name": "note.txt",
                "content": "hello upload\n",
                "role": "transcript_log",
                "content_type": "",
            },
        )
        self.assertEqual(upload.status_code, 400)
        self.assertEqual(upload.json()["error"]["message"], "content_type must not be empty")

    def test_runtime_worker_can_claim_and_complete(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_runtime", "capabilities": ["python"]})
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

    def test_runtime_worker_stages_job_attachments_into_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            artifact_store = get_artifact_store("localfs", ".agp-artifacts-tests")
            stored = artifact_store.write_text(
                namespace="control-plane",
                job_id="job_attach",
                name="note.txt",
                content="attached body",
                role="attachment",
            )
            runtime_client = RuntimeClient(
                RuntimeIdentity(runtime_id="rtm_worker_attach", hostname="localhost", server_url="http://testserver"),
                client=self.client,
            )
            worker = RuntimeSupervisor(
                runtime_client,
                host=InProcessTerminalHost(),
                adapter=DefaultAgentAdapter(),
                artifact_root=".agp-artifacts-tests",
                artifact_store=artifact_store,
            )
            session = TerminalSession(session_id="sess_attach", agent_id="agt_runtime_attach", workspace_ref=tmpdir)
            worker._stage_job_attachments(
                session=session,
                claimed={
                    "job_attachments": [
                        {
                            "artifact_id": "art_attach",
                            "name": "note.txt",
                            "storage_ref": stored.storage_ref,
                        }
                    ]
                },
            )
            runtime_client.close()
            staged = Path(tmpdir) / ".agp-tmp" / "attachments" / "art_attach" / "note.txt"
            self.assertTrue(staged.exists())
            self.assertEqual(staged.read_text(encoding="utf-8"), "attached body")

    def test_runtime_worker_stages_http_backed_job_attachments_via_control_plane_fetch(self) -> None:
        class NoReadArtifactStore:
            def read_text(self, *, storage_ref: str) -> str | None:
                return None

        with TemporaryDirectory() as tmpdir:
            runtime_client = RuntimeClient(
                RuntimeIdentity(runtime_id="rtm_worker_attach_http", hostname="localhost", server_url="http://testserver"),
                client=self.client,
            )
            worker = RuntimeSupervisor(
                runtime_client,
                host=InProcessTerminalHost(),
                adapter=DefaultAgentAdapter(),
                artifact_root=".agp-artifacts-tests",
                artifact_store=NoReadArtifactStore(),
            )
            fetch_calls: list[str] = []

            def _fetch_artifact_content(artifact_id: str) -> dict[str, str]:
                fetch_calls.append(artifact_id)
                return {"content": "fetched from cp"}

            runtime_client.fetch_artifact_content = _fetch_artifact_content  # type: ignore[method-assign]
            session = TerminalSession(session_id="sess_attach_http", agent_id="agt_runtime_attach_http", workspace_ref=tmpdir)
            worker._stage_job_attachments(
                session=session,
                claimed={
                    "job_attachments": [
                        {
                            "artifact_id": "art_attach_http",
                            "name": "note.txt",
                            "storage_ref": "agp://opaque",
                        }
                    ]
                },
            )
            runtime_client.close()
            staged = Path(tmpdir) / ".agp-tmp" / "attachments" / "art_attach_http" / "note.txt"
            self.assertEqual(fetch_calls, ["art_attach_http"])
            self.assertTrue(staged.exists())
            self.assertEqual(staged.read_text(encoding="utf-8"), "fetched from cp")

    def test_runtime_worker_cleanup_removes_staged_workspace_attachments(self) -> None:
        with TemporaryDirectory() as tmpdir:
            runtime_client = RuntimeClient(
                RuntimeIdentity(runtime_id="rtm_worker_cleanup", hostname="localhost", server_url="http://testserver"),
                client=self.client,
            )
            worker = RuntimeSupervisor(
                runtime_client,
                host=InProcessTerminalHost(),
                adapter=DefaultAgentAdapter(),
                artifact_root=".agp-artifacts-tests",
            )
            staged_root = Path(tmpdir) / ".agp-tmp" / "attachments" / "art_cleanup"
            staged_root.mkdir(parents=True)
            staged = staged_root / "staged.txt"
            staged.write_text("temp", encoding="utf-8")
            session = TerminalSession(
                session_id="sess_cleanup_attach",
                agent_id="agt_runtime_cleanup",
                workspace_ref=tmpdir,
                metadata={"staged_attachment_roots": [str(staged_root)]},
            )
            worker._cleanup_workspace(session, {"run": {"run_id": "run_cleanup"}})
            runtime_client.close()
            self.assertFalse(staged.exists())
            self.assertFalse(staged_root.exists())
            self.assertNotIn("staged_attachment_roots", session.metadata)

    def test_runtime_worker_staging_does_not_overwrite_workspace_files_with_same_basename(self) -> None:
        with TemporaryDirectory() as tmpdir:
            artifact_store = get_artifact_store("localfs", ".agp-artifacts-tests")
            stored = artifact_store.write_text(
                namespace="control-plane",
                job_id="job_attach_same_name",
                name="README.md",
                content="attached body",
                role="attachment",
            )
            original = Path(tmpdir) / "README.md"
            original.write_text("real workspace file", encoding="utf-8")
            runtime_client = RuntimeClient(
                RuntimeIdentity(runtime_id="rtm_worker_attach_same_name", hostname="localhost", server_url="http://testserver"),
                client=self.client,
            )
            worker = RuntimeSupervisor(
                runtime_client,
                host=InProcessTerminalHost(),
                adapter=DefaultAgentAdapter(),
                artifact_root=".agp-artifacts-tests",
                artifact_store=artifact_store,
            )
            session = TerminalSession(session_id="sess_attach_same_name", agent_id="agt_runtime_attach_same_name", workspace_ref=tmpdir)
            worker._stage_job_attachments(
                session=session,
                claimed={
                    "job_attachments": [
                        {
                            "artifact_id": "art_same_name",
                            "name": "README.md",
                            "storage_ref": stored.storage_ref,
                        }
                    ]
                },
            )
            runtime_client.close()
            staged = Path(tmpdir) / ".agp-tmp" / "attachments" / "art_same_name" / "README.md"
            self.assertEqual(original.read_text(encoding="utf-8"), "real workspace file")
            self.assertEqual(staged.read_text(encoding="utf-8"), "attached body")

    def test_runtime_worker_fails_promptly_when_attachment_staging_raises(self) -> None:
        class AttachmentHost(InProcessTerminalHost):
            def __init__(self, workspace_ref: str) -> None:
                super().__init__()
                self._workspace_ref = workspace_ref

            def get_or_create_session(self, *, agent_id: str, workspace_ref: str | None = None):
                session = super().get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
                session.workspace_ref = self._workspace_ref
                return session

        class FakeRuntimeClient:
            def __init__(self) -> None:
                self._log_fn = None
                self.identity = type(
                    "Identity",
                    (),
                    {"runtime_id": "rtm_stage_fail", "server_url": "http://example.invalid", "token": None, "metadata": {}},
                )()
                self.fail_calls: list[dict[str, object]] = []

            def register(self):
                return {"status": "ok"}

            def claim(self, *, agent_id, capability, lease_ttl_seconds):  # noqa: ARG002
                return {
                    "claimed": True,
                    "agent_id": agent_id,
                    "job": {"job_id": "job_stage_fail", "status": "running"},
                    "run": {"run_id": "run_stage_fail"},
                    "lease": {"lease_id": "lease_stage_fail", "fencing_token": 9},
                    "message": {"text": "review", "metadata": {}},
                    "job_attachments": [
                        {
                            "artifact_id": "art_stage_fail",
                            "name": "note.txt",
                            "storage_ref": "agp://broken",
                        }
                    ],
                }

            def heartbeat(self, **kwargs):  # noqa: ARG002
                return {"interrupt_requested": False}

            def progress(self, **kwargs):  # noqa: ARG002
                return {"status": "ok"}

            def fail(self, **kwargs):
                self.fail_calls.append(kwargs)
                return {"job_status": "failed"}

        with TemporaryDirectory() as tmpdir:
            artifact_store = get_artifact_store("localfs", ".agp-artifacts-tests")
            original_read_text = artifact_store.read_text

            def _raising_read_text(*, storage_ref: str) -> str | None:
                if storage_ref == "agp://broken":
                    raise RuntimeError("attachment backend exploded")
                return original_read_text(storage_ref=storage_ref)

            artifact_store.read_text = _raising_read_text  # type: ignore[method-assign]
            client = FakeRuntimeClient()
            worker = RuntimeSupervisor(
                client,
                host=AttachmentHost(tmpdir),
                adapter=DefaultAgentAdapter(),
                artifact_root=".agp-artifacts-tests",
                artifact_store=artifact_store,
            )
            outcome = worker.run_once(agent_id="agt_stage_fail")

        self.assertTrue(outcome["claimed"])
        self.assertIn("attachment backend exploded", outcome["error"])
        self.assertEqual(len(client.fail_calls), 1)

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
            if argv[2] == "send-text":
                return Result("")
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
        self.assertEqual(roles, ["prompt", "prompt", "transcript_log", "exec_log", "result"])
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
        self.client.post("/agents/up", json={"agent_id": "agt_runtime_registry", "capabilities": ["python"]})
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
        self.client.post("/agents/up", json={"agent_id": "agt_block", "capabilities": ["python"]})
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
        self.client.post("/agents/up", json={"agent_id": "agt_block_http", "capabilities": ["python"]})
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
        self.client.post("/agents/up", json={"agent_id": "agt_watch", "capabilities": ["python"]})
        sent = self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_watch"},
                "message": {"text": "watch me", "metadata": {}},
                "detach_policy": {"mode": "inline"},
            },
            headers={"Idempotency-Key": "watch-flow-1"},
        ).json()

        snapshots = self.agp.watch_job(
            sent["data"]["job_id"],
            poll_interval=0.0,
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
        self.client.post("/agents/up", json={"agent_id": "agt_orc", "capabilities": ["python"]})

        sent = self.agp.send(
            target_type="agent",
            target_id="agt_orc",
            text="orchestrate me",
            idempotency_key="orc-helper-1",
        )
        job_id = sent["job_id"]

        jobs = self.agp.list_jobs( target_agent_id="agt_orc")
        self.assertTrue(any(item["job_id"] == job_id for item in jobs["items"]))

        agents = self.agp.list_agents( capability="python")
        self.assertTrue(any(item["agent_id"] == "agt_orc" for item in agents["items"]))

        interrupted = self.agp.interrupt( job_id=job_id)
        self.assertEqual(interrupted["status"], "cancelled")

        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_orc",
            text="inline artifact",
            detach_mode="inline",
            idempotency_key="orc-helper-2",
        )
        artifact = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        content = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertIn("content", content)
        self.assertIn("inline result", content["content"])
        self.assertIn("storage_ref", content)

    def test_inmemory_artifact_backend_supports_inline_result_fetch(self) -> None:
        settings.artifact_backend = "inmemory"
        reset_artifact_store_state("inmemory")
        self.client.post("/agents/up", json={"agent_id": "agt_memart", "capabilities": ["python"]})

        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_memart",
            text="inline artifact in memory",
            detach_mode="inline",
            idempotency_key="mem-art-1",
        )
        artifact = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("mem://"))
        self.assertNotEqual(artifact["checksum"], "")
        content = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(content["storage_ref"], artifact["storage_ref"])
        self.assertIn("inline result", content["content"])

    def test_sharedfs_artifact_backend_supports_inline_result_fetch(self) -> None:
        settings.artifact_backend = "sharedfs"
        self.client.post("/agents/up", json={"agent_id": "agt_sharedart", "capabilities": ["python"]})

        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_sharedart",
            text="inline artifact in shared fs",
            detach_mode="inline",
            idempotency_key="shared-art-1",
        )
        artifact = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("agpfs://"))
        self.assertNotEqual(artifact["checksum"], "")
        content = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(content["storage_ref"], artifact["storage_ref"])
        self.assertIn("inline result", content["content"])

    def test_registryfs_artifact_backend_supports_inline_result_fetch(self) -> None:
        settings.artifact_backend = "registryfs"
        self.client.post("/agents/up", json={"agent_id": "agt_registryart", "capabilities": ["python"]})

        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_registryart",
            text="inline artifact in registry fs",
            detach_mode="inline",
            idempotency_key="registry-art-1",
        )
        artifact = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("agpr://"))
        self.assertNotEqual(artifact["checksum"], "")
        content = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(content["storage_ref"], artifact["storage_ref"])
        self.assertIn("inline result", content["content"])

    def test_s3_artifact_store_round_trip_uses_bucket_objects(self) -> None:
        class FakeS3Client:
            def __init__(self) -> None:
                self.buckets: set[str] = set()
                self.objects: dict[tuple[str, str], dict[str, object]] = {}

            def head_bucket(self, *, Bucket: str) -> None:
                if Bucket not in self.buckets:
                    raise RuntimeError("missing bucket")

            def create_bucket(self, **kwargs) -> None:
                self.buckets.add(str(kwargs["Bucket"]))

            def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str, Metadata: dict[str, str]) -> None:
                self.buckets.add(Bucket)
                self.objects[(Bucket, Key)] = {
                    "Body": Body,
                    "ContentType": ContentType,
                    "Metadata": Metadata,
                }

            def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
                item = self.objects[(Bucket, Key)]

                class BodyStream:
                    def __init__(self, payload: bytes) -> None:
                        self.payload = payload

                    def read(self) -> bytes:
                        return self.payload

                return {"Body": BodyStream(item["Body"])}

            def head_object(self, *, Bucket: str, Key: str) -> None:
                if (Bucket, Key) not in self.objects:
                    raise RuntimeError("missing object")

        fake_client = FakeS3Client()
        with patch.object(S3ArtifactStore, "_make_client", return_value=fake_client):
            store = S3ArtifactStore(
                bucket="agp-artifacts",
                endpoint_url="http://minio:9000",
                access_key_id="minioadmin",
                secret_access_key="minioadmin",
                region="us-east-1",
                force_path_style=True,
            )
            stored = store.write_text(
                namespace="rtm_test",
                job_id="job_test",
                name="result.txt",
                content="hello s3",
                role="result",
            )
            self.assertTrue(stored.storage_ref.startswith("s3://agp-artifacts/"))
            self.assertTrue(store.exists(storage_ref=stored.storage_ref))
            self.assertEqual(store.read_text(storage_ref=stored.storage_ref), "hello s3")
            self.assertEqual(stored.checksum, fake_client.objects[("agp-artifacts", stored.storage_ref.split("/", 3)[3])]["Metadata"]["checksum"])

    def test_s3_backend_requires_endpoint_configuration(self) -> None:
        with self.assertRaises(ValueError):
            Settings(
                artifact_backend="s3",
                s3_endpoint_url=None,
                s3_bucket="agp-artifacts",
                s3_access_key_id="key",
                s3_secret_access_key="secret",
            )

    def test_localfs_artifact_backend_populates_checksum(self) -> None:
        settings.artifact_backend = "localfs"
        self.client.post("/agents/up", json={"agent_id": "agt_localart", "capabilities": ["python"]})
        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_localart",
            text="inline artifact in local fs",
            detach_mode="inline",
            idempotency_key="local-art-1",
        )
        artifact = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"])
        self.assertEqual(artifact["kind"], "result")
        self.assertTrue(artifact["storage_ref"].startswith("file://"))
        self.assertNotEqual(artifact["checksum"], "")

    def test_job_and_run_artifact_listing_exposes_transcript_and_exec_roles(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_artlist", "capabilities": ["python"]})
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

        job_artifacts = self.agp.list_job_artifacts( job_id=sent["job_id"])
        run_artifacts = self.agp.list_run_artifacts( run_id=run_id)
        job_roles = {item["role"] for item in job_artifacts["items"]}
        run_roles = {item["role"] for item in run_artifacts["items"]}
        self.assertIn("transcript_log", job_roles)
        self.assertIn("exec_log", job_roles)
        self.assertIn("transcript_log", run_roles)
        self.assertIn("exec_log", run_roles)

        transcript_only = self.agp.list_job_artifacts( job_id=sent["job_id"], role="transcript_log")
        self.assertEqual(len(transcript_only["items"]), 1)
        self.assertEqual(transcript_only["items"][0]["role"], "transcript_log")
        transcript_content = self.agp.fetch_artifact(
            artifact_id=transcript_only["items"][0]["artifact_id"],
            content=True,
        )
        self.assertIn("transcript_log", transcript_content["content"])

    def test_observability_summary_reports_core_counts(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_obs", "capabilities": ["python"]})
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

        summary = self.agp.ops_health()
        self.assertGreaterEqual(summary["jobs"]["queued"], 1)
        self.assertGreaterEqual(summary["jobs"]["completed"], 1)
        self.assertGreaterEqual(summary["agents"]["idle"], 1)
        self.assertGreaterEqual(summary["queue"]["depth"], 1)
        self.assertGreater(summary["events"]["latest_event_seq"], 0)

    def test_observability_metrics_export_prometheus_text(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_metrics", "capabilities": ["python"]})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_metrics"},
                "message": {"text": "metrics export", "metadata": {}},
            },
            headers={"Idempotency-Key": "metrics-export-1"},
        )
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "capability", "id": "python"},
                "message": {"text": "capability backlog", "metadata": {}},
            },
            headers={"Idempotency-Key": "metrics-export-2"},
        )
        session = SessionLocal()
        try:
            direct_job = session.scalar(
                select(Job).where(
                    Job.target_queue == "agent:agt_metrics",
                    Job.status == "queued",
                )
            )
            capability_job = session.scalar(
                select(Job).where(
                    Job.target_queue == "capability:python",
                    Job.status == "queued",
                )
            )
            assert direct_job is not None
            assert capability_job is not None
            direct_job.updated_at = utc_now() - timedelta(seconds=185)
            capability_job.updated_at = utc_now() - timedelta(seconds=245)
            session.commit()
        finally:
            session.close()
        metrics = self.agp.ops_metrics()
        self.assertIn("# HELP agp_jobs_total", metrics)
        self.assertIn('agp_jobs_total{status="queued"}', metrics)
        self.assertIn("# HELP agp_queue_deliveries_total", metrics)
        self.assertIn('agp_queue_depth{agent_id="agt_metrics"} 1', metrics)
        self.assertIn("# HELP agp_queue_oldest_age_seconds", metrics)
        self.assertRegex(metrics, r'agp_queue_oldest_age_seconds\{agent_id="agt_metrics"\} 1[0-9]{2}(\.[0-9]+)?')
        self.assertRegex(metrics, r"(?m)^agp_queue_oldest_age_seconds 2[0-9]{2}(\.[0-9]+)?$")
        lines = metrics.splitlines()
        help_idx = lines.index("# HELP agp_queue_oldest_age_seconds Age in seconds of the oldest queued job.")
        type_idx = lines.index("# TYPE agp_queue_oldest_age_seconds gauge")
        sample_idx = next(idx for idx, line in enumerate(lines) if line.startswith('agp_queue_oldest_age_seconds{agent_id="agt_metrics"}'))
        self.assertLess(help_idx, sample_idx)
        self.assertLess(type_idx, sample_idx)
        self.assertIn("agp_events_latest_seq ", metrics)

    def test_agp_status_falls_back_to_public_health_when_ops_health_is_unavailable(self) -> None:
        import httpx
        from unittest.mock import patch

        request = httpx.Request("GET", "http://testserver/ops/health")
        response = httpx.Response(403, request=request)
        with patch.object(self.agp, "ops_health", side_effect=httpx.HTTPStatusError("forbidden", request=request, response=response)):
            result = self._cli_invoke(["status"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("CP reachable", result.output)

    def test_agents_api_counts_only_direct_queue_depth(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_depth", "capabilities": ["python"]})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_depth"},
                "message": {"text": "direct backlog", "metadata": {}},
            },
            headers={"Idempotency-Key": "agent-depth-direct"},
        )
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "capability", "id": "python"},
                "message": {"text": "cap backlog", "metadata": {}},
            },
            headers={"Idempotency-Key": "agent-depth-capability"},
        )
        session = SessionLocal()
        try:
            direct_job = session.scalar(
                select(Job).where(
                    Job.target_queue == "agent:agt_depth",
                    Job.status == "queued",
                )
            )
            capability_job = session.scalar(
                select(Job).where(
                    Job.target_queue == "capability:python",
                    Job.status == "queued",
                )
            )
            assert direct_job is not None
            assert capability_job is not None
            direct_job.updated_at = utc_now() - timedelta(seconds=125)
            session.commit()
        finally:
            session.close()

        agents = self.agp.list_agents()
        target = next(item for item in agents["items"] if item["agent_id"] == "agt_depth")
        self.assertEqual(target["queue_depth"], 1)
        self.assertIsNotNone(target["oldest_queued_at"])
        self.assertGreaterEqual(target["oldest_queue_age_seconds"], 120)

        agent = self.agp.get_agent("agt_depth")
        self.assertEqual(agent["queue_depth"], 1)
        self.assertIsNotNone(agent["oldest_queued_at"])
        self.assertGreaterEqual(agent["oldest_queue_age_seconds"], 120)

    def test_agp_ls_shows_pending_queue_depth_column(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_pending", "capabilities": ["python"]})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_pending"},
                "message": {"text": "pending one", "metadata": {}},
            },
            headers={"Idempotency-Key": "ls-pending-1"},
        )
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "capability", "id": "python"},
                "message": {"text": "pending two", "metadata": {}},
            },
            headers={"Idempotency-Key": "ls-pending-2"},
        )
        session = SessionLocal()
        try:
            direct_job = session.scalar(
                select(Job).where(
                    Job.target_queue == "agent:agt_pending",
                    Job.status == "queued",
                )
            )
            capability_job = session.scalar(
                select(Job).where(
                    Job.target_queue == "capability:python",
                    Job.status == "queued",
                )
            )
            assert direct_job is not None
            assert capability_job is not None
            direct_job.updated_at = utc_now() - timedelta(seconds=125)
            session.commit()
        finally:
            session.close()

        result = self._cli_invoke(["ls"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("PENDING", result.output)
        self.assertIn("QUEUE_AGE", result.output)
        self.assertRegex(result.output, r"agt_pending.*\b1\b.*02m:[0-5][0-9]s")

    def test_agp_ls_warns_when_queued_agent_has_no_runtime_bound(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_warn", "capabilities": ["python"]})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_warn"},
                "message": {"text": "blocked work", "metadata": {}},
            },
            headers={"Idempotency-Key": "ls-warn-1"},
        )

        session = SessionLocal()
        try:
            runtime = session.scalar(select(Runtime).where(Runtime.agent_id == "agt_warn"))
            assert runtime is not None
            runtime.agent_id = None
            session.commit()
        finally:
            session.close()

        result = self._cli_invoke(["ls"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[WARNINGS]", result.output)
        self.assertIn("agt_warn: 1 queued, no runtime bound.", result.output)

    def test_agp_status_agent_lookup(self) -> None:
        """status with an agent ID shows agent info."""
        self.client.post("/agents/up", json={"agent_id": "agt_status_queue", "capabilities": ["python"]})
        result = self._cli_invoke(["status", "agt_status_queue"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("AGENT:", result.output)
        self.assertIn("agt_status_queue", result.output)

    def test_agp_status_no_args_ping_with_queued_work(self) -> None:
        """status (no args) is a simple ping even when work is queued."""
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "capability", "id": "python"},
                "message": {"text": "capability backlog only", "metadata": {}},
            },
            headers={"Idempotency-Key": "status-capability-only"},
        )

        result = self._cli_invoke(["status"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("CP reachable", result.output)

    def test_agp_status_no_args_is_simple_ping(self) -> None:
        result = self._cli_invoke(["status"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("CP reachable", result.output)
        self.assertIn("agp health", result.output)

    def test_agents_api_real_cursor_flow_reaches_second_page(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_cursor_target", "capabilities": ["python"]})
        for idx in range(200):
            self.client.post(
                "/agents/up",
                json={"agent_id": f"agt_cursor_fill_{idx:03d}", "capabilities": ["python"]},
            )

        page1 = self.agp.list_agents(limit=200)
        self.assertEqual(len(page1["items"]), 200)
        self.assertIsNotNone(page1["page"]["next_cursor"])
        self.assertFalse(any(item["agent_id"] == "agt_cursor_target" for item in page1["items"]))

        page2 = self.agp.list_agents(limit=200, cursor=page1["page"]["next_cursor"])
        self.assertTrue(any(item["agent_id"] == "agt_cursor_target" for item in page2["items"]))
