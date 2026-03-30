"""Core agent/job/orchestration and artifact flows."""

from tests.mvp_flow.base import *


class MvpFlowCoreTest(MvpFlowTestBase):
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
        self.assertEqual(complete.status_code, 400)
        self.assertIn("result artifact is not valid JSON", complete.json()["error"]["message"])

        job = self.client.get(f"/jobs/{job_id}")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["data"]["status"], "failed")

        trace = self.client.get(f"/observability/jobs/{job_id}/trace")
        self.assertEqual(trace.status_code, 200)
        self.assertEqual(trace.json()["data"]["runs"][-1]["status"], "failed")

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

        summary = self.agp.observability_summary()
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
        metrics = self.agp.observability_metrics()
        self.assertIn("# HELP agp_jobs_total", metrics)
        self.assertIn('agp_jobs_total{status="queued"}', metrics)
        self.assertIn("# HELP agp_queue_deliveries_total", metrics)
        self.assertIn("agp_events_latest_seq ", metrics)
