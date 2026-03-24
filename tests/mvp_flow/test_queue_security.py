"""Queueing, handoff, auth, security, and compatibility flows."""

from tests.mvp_flow.base import *


class MvpFlowQueueSecurityTest(MvpFlowTestBase):
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

        deliveries = self.agp.list_deliveries( state="dead_lettered", job_id=sent["job_id"])
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
            agp_protected = AgpClient(http_client=protected_client)
            status_before = agp_protected.auth_status()
            self.assertEqual(status_before["operator"]["managed_token_count"], 2)
            self.assertEqual(status_before["runtime"]["active_token_count"], 1)

            rotated_operator = agp_protected.rotate_operator_tokens(
                operator_bearer_token=None,
                operator_token_roles_json={
                    "viewer2": "read_only",
                    "ops2": "operator",
                    "admin2": "security_admin",
                },
            )
            self.assertEqual(rotated_operator["managed_token_count"], 3)
            protected_client.headers.update({"Authorization": "Bearer admin2"})

            rotated_runtime = agp_protected.rotate_runtime_tokens(
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
        holder: dict[str, object] = {}

        def run_worker() -> None:
            try:
                holder["payload"] = worker.run_once(
                    agent_id="agt_interrupt",
                    heartbeat_interval_seconds=0.01,
                )
            except Exception as exc:  # pragma: no cover - surfaced by the test assertions below
                holder["error"] = exc

        thread = Thread(target=run_worker)
        thread.start()
        try:
            sleep(0.03)
            interrupt = None
            for _ in range(20):
                try:
                    interrupt = self.client.post(f"/jobs/{job_id}/interrupt")
                    break
                except Exception as exc:
                    if "database is locked" not in str(exc):
                        raise
                    sleep(0.02)
            self.assertIsNotNone(interrupt)
            assert interrupt is not None
            self.assertEqual(interrupt.status_code, 200)
        finally:
            thread.join(timeout=2.0)
            runtime_client.close()

        self.assertFalse(thread.is_alive())
        if "error" in holder:
            raise holder["error"]  # type: ignore[misc]

        payload = holder["payload"]
        assert isinstance(payload, dict)
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
        agents = self.agp.list_agents( status="terminated")
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
