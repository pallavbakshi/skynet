"""Observability, backup, restore, drills, and upgrade flows."""

from tests.mvp_flow.base import *
from agp.migrations import _discover_migrations


def _current_schema_tag() -> str:
    """Derive the expected schema version from the latest discovered migration."""
    migrations = _discover_migrations()
    return migrations[-1][0] if migrations else "0001_initial"


class MvpFlowObservabilityTest(MvpFlowTestBase):
    def test_observability_job_trace_reports_ordered_timeline_and_durations(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_trace", "capabilities": ["python"]})
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
            artifact_root=str(settings.artifact_root),
        )
        try:
            payload = worker.run_once(agent_id="agt_trace")
        finally:
            runtime_client.close()

        job_id = payload["claim"]["job"]["job_id"]
        trace = self.agp.job_trace( job_id=job_id)
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
        self.client.post("/agents/up", json={"agent_id": "agt_logs", "capabilities": ["python"]})
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
            artifact_root=str(settings.artifact_root),
        )
        try:
            payload = worker.run_once(agent_id="agt_logs")
        finally:
            runtime_client.close()

        job_id = payload["claim"]["job"]["job_id"]
        logs = self.agp.logs_control_plane( limit=200)
        self.assertTrue(Path(logs["source"]).name.endswith("control-plane.jsonl"))
        items = logs["items"]
        self.assertGreaterEqual(len(items), 1)
        related = [item for item in items if item.get("job_id") == job_id]
        self.assertTrue(any(item["event_type"] == "job.accepted" for item in related))
        self.assertTrue(any(item["event_type"] == "run.completed" for item in related))
        self.assertTrue(all(item["kind"] == "control_plane_event" for item in related))

    def test_observability_control_plane_logs_span_rotated_files(self) -> None:
        settings.observability_log_rotation_bytes = 1
        self.client.post("/agents/up", json={"agent_id": "agt_logs_rot", "capabilities": ["python"]})
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

        logs = self.agp.logs_control_plane( limit=200)
        event_types = {item["event_type"] for item in logs["items"] if item.get("kind") == "control_plane_event"}
        self.assertIn("agent.registered", event_types)
        self.assertIn("job.accepted", event_types)

    def test_observability_runtime_logs_report_supervision_entries(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_runtime_logs", "capabilities": ["python"]})
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
            artifact_root=str(settings.artifact_root),
        )
        try:
            payload = worker.run_once(agent_id="agt_runtime_logs")
        finally:
            runtime_client.close()

        self.assertTrue(payload["claimed"])
        logs = self.agp.logs_runtime( runtime_id="rtm_logs_api", limit=200)
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
            self.client.post("/agents/up", json={"agent_id": agent_id, "capabilities": ["python"]})
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

        self.client.post("/agents/up", json={"agent_id": "agt_alerts_dead", "capabilities": ["python"]})
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

        alerts = self.agp.ops_alerts()
        codes = {item["code"] for item in alerts["items"]}
        self.assertIn("queue_dead_lettering", codes)
        self.assertIn("rising_terminal_failure_rate", codes)
        self.assertGreaterEqual(alerts["counts"]["dead_lettered_deliveries"], 1)

    def test_observability_alerts_report_high_per_agent_queue_depth(self) -> None:
        settings.observability_queue_depth_alert_threshold = 2
        try:
            self.client.post("/agents/up", json={"agent_id": "agt_queue_alert", "capabilities": ["python"]})
            for idx in range(2):
                self.client.post(
                    "/messages/send",
                    json={
                        "target": {"type": "agent", "id": "agt_queue_alert"},
                        "message": {"text": f"queued {idx}", "metadata": {}},
                    },
                    headers={"Idempotency-Key": f"queue-alert-{idx}"},
                )

            alerts = self.agp.ops_alerts()
            queue_alert = next(item for item in alerts["items"] if item["code"] == "queue_depth_high")
            self.assertEqual(queue_alert["evidence"]["threshold"], 2)
            self.assertEqual(queue_alert["evidence"]["max_queue_depth"], 2)
            self.assertEqual(queue_alert["evidence"]["affected_agents"][0]["agent_id"], "agt_queue_alert")
            self.assertEqual(queue_alert["evidence"]["affected_agents"][0]["queue_depth"], 2)
            self.assertEqual(alerts["counts"]["queue_depth_breaches"], 1)
        finally:
            settings.observability_queue_depth_alert_threshold = 5

    def test_ops_alerts_report_global_capability_queue_depth_high(self) -> None:
        settings.observability_queue_depth_alert_threshold = 2
        try:
            for idx in range(2):
                self.client.post(
                    "/messages/send",
                    json={
                        "target": {"type": "capability", "id": "python"},
                        "message": {"text": f"shared backlog {idx}", "metadata": {}},
                    },
                    headers={"Idempotency-Key": f"shared-queue-alert-{idx}"},
                )

            alerts = self.agp.ops_alerts()
            queue_alert = next(item for item in alerts["items"] if item["code"] == "queue_depth_global_high")
            self.assertEqual(queue_alert["evidence"]["threshold"], 2)
            self.assertEqual(queue_alert["evidence"]["queue_depth_total"], 2)
            self.assertEqual(queue_alert["evidence"]["direct_queue_depth_total"], 0)
            self.assertEqual(queue_alert["evidence"]["shared_queue_depth"], 2)
            self.assertEqual(alerts["counts"]["global_queue_depth_breaches"], 1)
        finally:
            settings.observability_queue_depth_alert_threshold = 5

    def test_ops_alerts_do_not_raise_global_queue_alert_for_tiny_shared_backlog(self) -> None:
        settings.observability_queue_depth_alert_threshold = 2
        try:
            self.client.post("/agents/up", json={"agent_id": "agt_mixed_queue_alert", "capabilities": ["python"]})
            for idx in range(2):
                self.client.post(
                    "/messages/send",
                    json={
                        "target": {"type": "agent", "id": "agt_mixed_queue_alert"},
                        "message": {"text": f"direct backlog {idx}", "metadata": {}},
                    },
                    headers={"Idempotency-Key": f"direct-queue-alert-{idx}"},
                )
            self.client.post(
                "/messages/send",
                json={
                    "target": {"type": "capability", "id": "python"},
                    "message": {"text": "shared backlog", "metadata": {}},
                },
                headers={"Idempotency-Key": "shared-queue-alert-small"},
            )

            alerts = self.agp.ops_alerts()
            codes = {item["code"] for item in alerts["items"]}
            self.assertIn("queue_depth_high", codes)
            self.assertNotIn("queue_depth_global_high", codes)
            self.assertEqual(alerts["counts"]["queue_depth_breaches"], 1)
            self.assertEqual(alerts["counts"]["global_queue_depth_breaches"], 0)
        finally:
            settings.observability_queue_depth_alert_threshold = 5

    def test_observability_alerts_report_stale_queued_jobs(self) -> None:
        settings.observability_stale_queue_age_seconds = 120
        try:
            self.client.post(
                "/messages/send",
                json={
                    "target": {"type": "capability", "id": "python"},
                    "message": {"text": "stale queue", "metadata": {}},
                },
                headers={"Idempotency-Key": "stale-queue-1"},
            )
            session = SessionLocal()
            try:
                job = session.scalar(
                    select(Job).where(
                        Job.target_queue == "capability:python",
                        Job.status == "queued",
                    )
                )
                assert job is not None
                job.updated_at = utc_now() - timedelta(seconds=185)
                session.commit()
            finally:
                session.close()

            alerts = self.agp.ops_alerts()
            stale_alert = next(item for item in alerts["items"] if item["code"] == "stale_queued_jobs")
            self.assertEqual(stale_alert["evidence"]["threshold_seconds"], 120)
            self.assertEqual(stale_alert["evidence"]["affected_agents"], [])
            self.assertGreaterEqual(stale_alert["evidence"]["max_oldest_queue_age_seconds"], 180)
            self.assertIsNotNone(stale_alert["evidence"]["global_oldest_queued_at"])
            self.assertEqual(alerts["counts"]["stale_queue_agents"], 0)
            self.assertEqual(alerts["counts"]["stale_queued_work"], 1)
        finally:
            settings.observability_stale_queue_age_seconds = 900

    def test_observability_stale_queue_alert_clears_after_stale_delivery_redrive(self) -> None:
        settings.observability_stale_queue_age_seconds = 120
        try:
            self.client.post("/agents/up", json={"agent_id": "agt_redrive_alert", "capabilities": ["python"]})
            self.client.post(
                "/messages/send",
                json={
                    "target": {"type": "agent", "id": "agt_redrive_alert"},
                    "message": {"text": "stale then redrive", "metadata": {}},
                },
                headers={"Idempotency-Key": "stale-redrive-alert-1"},
            )
            session = SessionLocal()
            try:
                backend = get_queue_backend(settings.queue_backend)
                job = session.scalar(
                    select(Job).where(
                        Job.target_queue == "agent:agt_redrive_alert",
                        Job.status == "queued",
                    )
                )
                assert job is not None
                job.updated_at = utc_now() - timedelta(seconds=185)
                session.commit()
                initial_alerts = self.agp.ops_alerts()
                stale_alert = next(item for item in initial_alerts["items"] if item["code"] == "stale_queued_jobs")
                self.assertGreaterEqual(stale_alert["evidence"]["max_oldest_queue_age_seconds"], 180)

                delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_redrive_alert"])
                self.assertIsNotNone(delivery)
                session.commit()
                record = session.get(QueueDeliveryRecord, delivery.delivery_id)
                assert record is not None
                record.last_delivered_at = utc_now() - timedelta(seconds=185)
                session.commit()
                result = backend.redrive_stale_deliveries(
                    session,
                    visibility_timeout_seconds=120,
                    max_delivery_attempts=3,
                )
                session.commit()
                self.assertEqual(result["redriven_deliveries"], 1)
            finally:
                session.close()

            alerts = self.agp.ops_alerts()
            self.assertFalse(any(item["code"] == "stale_queued_jobs" for item in alerts["items"]))
            self.assertEqual(alerts["counts"]["stale_queued_work"], 0)
            self.assertEqual(alerts["counts"]["stale_queue_agents"], 0)
        finally:
            settings.observability_stale_queue_age_seconds = 900

    def test_observability_triage_includes_queue_backlog_age_by_agent(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_triage_queue", "capabilities": ["python"]})
        self.client.post(
            "/messages/send",
            json={
                "target": {"type": "agent", "id": "agt_triage_queue"},
                "message": {"text": "queued for triage", "metadata": {}},
            },
            headers={"Idempotency-Key": "triage-queue-age-1"},
        )
        session = SessionLocal()
        try:
            job = session.scalar(
                select(Job).where(
                    Job.target_queue == "agent:agt_triage_queue",
                    Job.status == "queued",
                )
            )
            assert job is not None
            job.updated_at = utc_now() - timedelta(seconds=185)
            session.commit()
        finally:
            session.close()

        triage = self.agp.ops_triage()
        backlog = triage["queue_backlog_by_agent"]["agt_triage_queue"]
        self.assertEqual(backlog["depth"], 1)
        self.assertIsNotNone(backlog["oldest_queued_at"])
        self.assertGreaterEqual(backlog["oldest_queue_age_seconds"], 180)

    def test_observability_alert_thresholds_are_configurable(self) -> None:
        settings.observability_unreachable_runtime_threshold = 2
        settings.observability_expired_lease_alert_threshold = 10
        settings.observability_dead_letter_alert_threshold = 2
        settings.observability_terminal_failure_sample_size = 5
        settings.observability_terminal_failure_rate_threshold = 0.9
        settings.observability_queue_depth_alert_threshold = 2
        settings.observability_stale_queue_age_seconds = 9999

        try:
            self.client.post("/runtimes/register", json={"runtime_id": "rtm_alert_cfg", "hostname": "localhost"})
            session = SessionLocal()
            try:
                runtime = session.get(Runtime, "rtm_alert_cfg")
                assert runtime is not None
                runtime.health_status = HealthStatus.UNREACHABLE.value
                session.commit()
            finally:
                session.close()

            self.client.post("/agents/up", json={"agent_id": "agt_alert_cfg", "capabilities": ["python"]})
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

            alerts = self.agp.ops_alerts()
            codes = {item["code"] for item in alerts["items"]}
            self.assertNotIn("runtime_unreachable", codes)
            self.assertNotIn("heartbeat_loss_spike", codes)
            self.assertNotIn("repeated_fencing_events", codes)
            self.assertNotIn("queue_dead_lettering", codes)
            self.assertNotIn("rising_terminal_failure_rate", codes)
            self.assertNotIn("queue_depth_high", codes)
        finally:
            settings.observability_queue_depth_alert_threshold = 5
            settings.observability_stale_queue_age_seconds = 900

    def test_observability_dispatch_alerts_posts_to_configured_webhook(self) -> None:
        settings.observability_alert_webhook_url = "https://alerts.example.invalid/agp"
        try:
            self.client.post("/runtimes/register", json={"runtime_id": "rtm_alert_dispatch", "hostname": "localhost"})
            session = SessionLocal()
            try:
                runtime = session.get(Runtime, "rtm_alert_dispatch")
                assert runtime is not None
                runtime.health_status = HealthStatus.UNREACHABLE.value
                session.commit()
            finally:
                session.close()

            sink: list[dict] = []
            with patch.object(control_plane_module.httpx, "Client", return_value=_FakeWebhookClient(sink)):
                payload = self.agp.observability_dispatch_alerts()

            self.assertTrue(payload["delivered"])
            self.assertEqual(payload["target"], settings.observability_alert_webhook_url)
            self.assertEqual(len(sink), 1)
            self.assertEqual(sink[0]["url"], settings.observability_alert_webhook_url)
            self.assertEqual(sink[0]["json"]["kind"], "observability_alerts")
            codes = {item["code"] for item in sink[0]["json"]["items"]}
            self.assertIn("runtime_unreachable", codes)
        finally:
            settings.observability_alert_webhook_url = None

    def test_backup_and_restore_snapshot_preserves_state_and_artifacts(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_backup", "capabilities": ["python"]})
        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_backup",
            text="backup this artifact",
            detach_mode="inline",
            idempotency_key="backup-1",
        )
        artifact_before = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertIn("inline result", artifact_before["content"])

        backup_dir = Path(mkdtemp(prefix="agp-backup-"))
        create_backup_snapshot(backup_dir=backup_dir)

        from tests._base import _reset_sqlite_database
        engine.dispose()
        _reset_sqlite_database()
        if settings.artifact_root.exists():
            shutil.rmtree(settings.artifact_root)
        init_db()

        missing = self.client.get(f"/jobs/{inline_sent['job_id']}")
        self.assertEqual(missing.status_code, 404)

        restore_backup_snapshot(backup_dir=backup_dir, require_stopped_local_cp=False)

        restored_job = self.client.get(f"/jobs/{inline_sent['job_id']}")
        self.assertEqual(restored_job.status_code, 200)
        self.assertEqual(restored_job.json()["data"]["status"], "completed")
        restored_artifact = self.agp.fetch_artifact( artifact_id=inline_sent["result_artifact_id"], content=True)
        self.assertEqual(restored_artifact["storage_ref"], artifact_before["storage_ref"])
        self.assertIn("inline result", restored_artifact["content"])

    def test_validate_restored_state_reports_missing_artifacts(self) -> None:
        self.client.post("/agents/up", json={"agent_id": "agt_validate", "capabilities": ["python"]})
        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_validate",
            text="validate restored artifact",
            detach_mode="inline",
            idempotency_key="validate-restore-1",
        )
        artifact_before = self.agp.fetch_artifact(
            artifact_id=inline_sent["result_artifact_id"],
            content=True,
        )
        self.assertIn("inline result", artifact_before["content"])
        backup_dir = Path(mkdtemp(prefix="agp-backup-validate-"))
        create_backup_snapshot(backup_dir=backup_dir)

        restore_backup_snapshot(backup_dir=backup_dir, require_stopped_local_cp=False)
        shutil.rmtree(settings.artifact_root)
        settings.artifact_root.mkdir(parents=True, exist_ok=True)

        validation = validate_restored_state()
        self.assertFalse(validation["ok"])
        self.assertGreaterEqual(validation["missing_artifacts"], 1)
        missing_ids = {item["artifact_id"] for item in validation["missing"]}
        self.assertIn(inline_sent["result_artifact_id"], missing_ids)

    def test_reconstruct_queue_from_state_rebuilds_pending_delivery_and_dedupes_stale_rows(self) -> None:
        settings.queue_backend = "delivery_table"
        self.client.post("/agents/up", json={"agent_id": "agt_reconstruct", "capabilities": ["python"]})
        sent = self.agp.send(
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
        self.client.post("/agents/up", json={"agent_id": "agt_dr", "capabilities": ["python"]})
        inline_sent = self.agp.send(
            target_type="agent",
            target_id="agt_dr",
            text="dr inline artifact",
            detach_mode="inline",
            idempotency_key="dr-inline-1",
        )
        queued_sent = self.agp.send(
            target_type="agent",
            target_id="agt_dr",
            text="dr queued work",
            idempotency_key="dr-queued-1",
        )

        backup_dir = Path(mkdtemp(prefix="agp-backup-recover-"))
        create_backup_snapshot(backup_dir=backup_dir)

        from tests._base import _reset_sqlite_database
        engine.dispose()
        _reset_sqlite_database()
        if settings.artifact_root.exists():
            shutil.rmtree(settings.artifact_root)
        init_db()

        recovered = restore_and_recover_snapshot(
            backup_dir=backup_dir,
            require_stopped_local_cp=False,
        )
        self.assertTrue(recovered["ok"])
        self.assertGreaterEqual(recovered["validation"]["checked_artifacts"], 1)
        self.assertEqual(recovered["validation"]["missing_artifacts"], 0)
        self.assertGreaterEqual(recovered["queue_reconstruction"]["reconstructed_jobs"], 1)

        restored_artifact = self.agp.fetch_artifact(
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
        expected_version = _current_schema_tag()
        initial = get_upgrade_status()
        self.assertEqual(initial["schema_version"], expected_version)
        self.assertEqual(initial["release_version"], "0.1.0")
        self.assertIsNone(initial["previous_release_version"])

        updated = mark_upgrade(schema_version="0002_queue_backends", release_version="0.2.0")
        self.assertEqual(updated["schema_version"], "0002_queue_backends")
        self.assertEqual(updated["release_version"], "0.2.0")
        self.assertEqual(updated["previous_schema_version"], expected_version)
        self.assertEqual(updated["previous_release_version"], "0.1.0")
        self.assertEqual(updated["rollback_target_release_version"], "0.1.0")

    def test_upgrade_rollback_restores_immediately_previous_versions(self) -> None:
        expected_version = _current_schema_tag()
        mark_upgrade(schema_version="0002_queue_backends", release_version="0.2.0")
        rolled_back = rollback_to_previous_version()
        self.assertEqual(rolled_back["release_version"], "0.1.0")
        self.assertEqual(rolled_back["schema_version"], expected_version)
        self.assertEqual(rolled_back["previous_release_version"], "0.2.0")
        self.assertEqual(rolled_back["previous_schema_version"], "0002_queue_backends")

    def test_upgrade_rollback_rejects_when_no_previous_target_exists(self) -> None:
        with self.assertRaises(ValueError):
            rollback_to_previous_version()

    def test_upgrade_status_is_exposed_over_operator_api(self) -> None:
        expected_version = _current_schema_tag()
        response = self.client.get("/system/upgrade-status")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["schema_version"], expected_version)
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
