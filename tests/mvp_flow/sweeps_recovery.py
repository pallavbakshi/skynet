"""Lease expiry, sweeps, recovery, and routing flows."""

from .base import *


class MvpFlowSweepsRecoveryTest(MvpFlowTestBase):
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
        agents = self.agp.list_agents( status="terminated")
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
        agents = self.agp.list_agents( status="draining")
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

    def test_idle_claim_poll_refreshes_runtime_activity(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_poll", "hostname": "localhost"})
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_poll", "capability_id": "cap_python", "assigned_runtime_id": "rtm_poll"},
        )

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_poll")
            assert runtime is not None
            runtime.last_seen_at = utc_now() - timedelta(seconds=600)
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=600)
            session.commit()
        finally:
            session.close()

        claim = self.client.post("/runs/claim", json={"runtime_id": "rtm_poll", "agent_id": "agt_poll"})
        self.assertEqual(claim.status_code, 200)
        self.assertFalse(claim.json()["data"]["claimed"])

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_poll")
            assert runtime is not None
            self.assertIsNotNone(runtime.last_heartbeat_at)
            result = sweep_stale_runtimes(session, now=utc_now(), stale_timeout_seconds=90, degraded_timeout_seconds=45)
            self.assertEqual(result["offline_runtimes"], 0)
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
