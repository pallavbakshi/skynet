"""Gap/regression coverage added after the initial MVP flow suite."""

from tests.mvp_flow.base import *


class MvpFlowGapRegressionTest(MvpFlowTestBase):
    def test_gap1_deterministic_capability_pool_routing_selects_lru(self) -> None:
        """Gap 1: Capability-pool routing must use deterministic LRU tie-breaking."""
        from agp.models import CapabilityPool

        session = SessionLocal()
        try:
            session.add(Capability(
                capability_id="cap_pool_test",
                name="Pool Test",
                version="v1",
                image_ref="test:v1",
                model_ref="test",
                resource_tier="small",
                permission_profile="default",
                queue_mode="capability_pool",
                runtime_requirements_json={},
                created_at=utc_now(),
                updated_at=utc_now(),
            ))
            session.flush()
            session.add(CapabilityPool(
                capability_id="cap_pool_test",
                queue_id="capability:cap_pool_test:v1",
                routing_policy="least_recent",
            ))
            session.commit()
        finally:
            session.close()

        for i, agent_id in enumerate(["agt_pool_c", "agt_pool_a", "agt_pool_b"]):
            resp = self.client.post("/agents/up", json={"agent_id": agent_id, "capability_id": "cap_pool_test"})
            self.assertEqual(resp.status_code, 200)
            session = SessionLocal()
            try:
                agent = session.get(Agent, agent_id)
                agent.last_seen_at = utc_now() - timedelta(seconds=100 - i * 10)
                session.commit()
            finally:
                session.close()

        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_pool", "hostname": "pool-host"})
        self.assertEqual(runtime.status_code, 200)

        sent = self.client.post("/messages/send", json={
            "target": {"type": "capability", "id": "cap_pool_test"},
            "message": {"text": "pool work"},
        })
        self.assertEqual(sent.status_code, 200)

        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_pool",
            "capability_id": "cap_pool_test",
            "lease_ttl_seconds": 30,
        })
        self.assertEqual(claim.status_code, 200)
        data = claim.json()["data"]
        self.assertTrue(data["claimed"])
        self.assertEqual(data["agent_id"], "agt_pool_c")

        job_id = data["job"]["job_id"]
        events = self.client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
        routing_events = [e for e in events if e["event_type"] == "routing.decision"]
        self.assertEqual(len(routing_events), 1)
        self.assertEqual(routing_events[0]["body"]["policy"], "least_recent")
        self.assertEqual(routing_events[0]["body"]["selected_agent_id"], "agt_pool_c")
        self.assertEqual(routing_events[0]["body"]["candidate_count"], 3)

    def test_gap1_deterministic_routing_is_stable(self) -> None:
        """Gap 1: Repeated claims with same state must pick the same agent."""
        from agp.models import CapabilityPool

        session = SessionLocal()
        try:
            session.add(Capability(
                capability_id="cap_stable",
                name="Stable",
                version="v1",
                image_ref="test:v1",
                model_ref="test",
                resource_tier="small",
                permission_profile="default",
                queue_mode="capability_pool",
                runtime_requirements_json={},
                created_at=utc_now(),
                updated_at=utc_now(),
            ))
            session.flush()
            session.add(CapabilityPool(
                capability_id="cap_stable",
                queue_id="capability:cap_stable:v1",
                routing_policy="least_recent",
            ))
            session.commit()
        finally:
            session.close()

        for agent_id in ["agt_s1", "agt_s2"]:
            self.client.post("/agents/up", json={"agent_id": agent_id, "capability_id": "cap_stable"})

        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_stable", "hostname": "h"})
        self.assertEqual(runtime.status_code, 200)

        selected_agents = []
        for _ in range(2):
            self.client.post("/messages/send", json={
                "target": {"type": "capability", "id": "cap_stable"},
                "message": {"text": "stable work"},
            })
            claim = self.client.post("/runs/claim", json={
                "runtime_id": "rtm_stable",
                "capability_id": "cap_stable",
                "lease_ttl_seconds": 30,
            })
            data = claim.json()["data"]
            if data["claimed"]:
                selected_agents.append(data["agent_id"])
                run_id = data["run"]["run_id"]
                artifacts = self._materialize_terminal_artifacts({
                    "prompt.txt": "prompt",
                    "transcript.txt": "transcript_log",
                    "exec.txt": "exec_log",
                    "result.txt": "result",
                })
                self.client.post(f"/runs/{run_id}/complete", json={
                    "runtime_id": "rtm_stable",
                    "lease_id": data["lease"]["lease_id"],
                    "fencing_token": data["lease"]["fencing_token"],
                    "artifacts": artifacts,
                })

        self.assertEqual(len(selected_agents), 2)
        self.assertEqual(selected_agents[0], selected_agents[1])

    def test_gap2_handoff_rejects_invalid_artifact_id(self) -> None:
        """Gap 2: Handoff must reject artifact IDs that don't exist."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_ho1", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)

        sent = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_ho1"},
            "message": {"text": "handoff source"},
        })
        job_id = sent.json()["data"]["job_id"]

        handoff = self.client.post(f"/jobs/{job_id}/handoff", json={
            "targets": [{"type": "agent", "id": "agt_ho1"}],
            "message": {"text": "handoff follow-up"},
            "artifact_ids": ["art_nonexistent"],
        })
        self.assertEqual(handoff.status_code, 400)
        self.assertIn("not found", handoff.json()["error"]["message"])

    def test_gap2_handoff_rejects_artifacts_from_wrong_job(self) -> None:
        """Gap 2: Handoff must reject artifacts that belong to a different job."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_ho2", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_ho2", "hostname": "h"})
        self.assertEqual(runtime.status_code, 200)

        sent1 = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_ho2"},
            "message": {"text": "job 1"},
        })
        job1_id = sent1.json()["data"]["job_id"]

        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_ho2", "agent_id": "agt_ho2", "lease_ttl_seconds": 30,
        })
        data = claim.json()["data"]
        self.assertTrue(data["claimed"])
        artifacts = self._materialize_terminal_artifacts({
            "prompt.txt": "prompt",
            "transcript.txt": "transcript_log",
            "exec.txt": "exec_log",
            "result.txt": "result",
        })
        run_id = data["run"]["run_id"]
        self.client.post(f"/runs/{run_id}/complete", json={
            "runtime_id": "rtm_ho2",
            "lease_id": data["lease"]["lease_id"],
            "fencing_token": data["lease"]["fencing_token"],
            "artifacts": artifacts,
        })

        job1_artifacts = self.client.get(f"/jobs/{job1_id}/artifacts").json()["data"]["items"]
        self.assertTrue(len(job1_artifacts) > 0)
        art_id_from_job1 = job1_artifacts[0]["artifact_id"]

        sent2 = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_ho2"},
            "message": {"text": "job 2"},
        })
        job2_id = sent2.json()["data"]["job_id"]

        handoff = self.client.post(f"/jobs/{job2_id}/handoff", json={
            "targets": [{"type": "agent", "id": "agt_ho2"}],
            "message": {"text": "bad handoff"},
            "artifact_ids": [art_id_from_job1],
        })
        self.assertEqual(handoff.status_code, 400)
        self.assertIn("does not belong", handoff.json()["error"]["message"])

    def test_gap3_runtime_list_includes_claimed_work(self) -> None:
        """Gap 3: GET /runtimes must include active claimed work per runtime."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_cw", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_cw", "hostname": "host-cw"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_cw"},
            "message": {"text": "work"},
        })
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_cw", "agent_id": "agt_cw", "lease_ttl_seconds": 30,
        })
        self.assertTrue(claim.json()["data"]["claimed"])

        # List runtimes and check claimed_work is present
        runtimes = self.client.get("/runtimes").json()["data"]["items"]
        rtm = next(r for r in runtimes if r["runtime_id"] == "rtm_cw")
        self.assertIn("claimed_work", rtm)
        self.assertEqual(rtm["active_run_count"], 1)
        self.assertEqual(len(rtm["claimed_work"]), 1)
        self.assertEqual(rtm["claimed_work"][0]["agent_id"], "agt_cw")

    def test_gap3_runtime_detail_endpoint(self) -> None:
        """Gap 3: GET /runtimes/{runtime_id} returns claimed work and agents."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_rd", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_rd", "hostname": "host-rd"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_rd"},
            "message": {"text": "detail work"},
        })
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_rd", "agent_id": "agt_rd", "lease_ttl_seconds": 30,
        })
        self.assertTrue(claim.json()["data"]["claimed"])

        detail = self.client.get("/runtimes/rtm_rd").json()["data"]
        self.assertEqual(detail["runtime_id"], "rtm_rd")
        self.assertEqual(detail["active_run_count"], 1)
        self.assertTrue(len(detail["claimed_work"]) > 0)
        self.assertIn("fencing_token", detail["claimed_work"][0])
        self.assertTrue(len(detail["agents"]) > 0)

    def test_gap4_artifact_content_pagination(self) -> None:
        """Gap 4: Artifact content endpoint supports offset/limit pagination."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_pg", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_pg", "hostname": "host-pg"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_pg"},
            "message": {"text": "pagination work"},
        })
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_pg", "agent_id": "agt_pg", "lease_ttl_seconds": 30,
        })
        data = claim.json()["data"]
        artifacts = self._materialize_terminal_artifacts({
            "prompt.txt": "prompt",
            "transcript.txt": "transcript_log",
            "exec.txt": "exec_log",
            "result.txt": "result",
        })
        self.client.post(f"/runs/{data['run']['run_id']}/complete", json={
            "runtime_id": "rtm_pg",
            "lease_id": data["lease"]["lease_id"],
            "fencing_token": data["lease"]["fencing_token"],
            "artifacts": artifacts,
        })

        job_id = data["job"]["job_id"]
        job_arts = self.client.get(f"/jobs/{job_id}/artifacts").json()["data"]["items"]
        art_id = job_arts[0]["artifact_id"]

        # Full read
        full = self.client.get(f"/artifacts/{art_id}/content").json()["data"]
        self.assertIn("content", full)
        self.assertIn("total_length", full)
        self.assertIn("size_bytes", full)
        self.assertFalse(full["has_more"])

        # Paginated read
        page = self.client.get(f"/artifacts/{art_id}/content?offset=0&limit=5").json()["data"]
        self.assertEqual(len(page["content"]), 5)
        self.assertEqual(page["offset"], 0)
        self.assertEqual(page["limit"], 5)
        if full["total_length"] > 5:
            self.assertTrue(page["has_more"])

    def test_gap5_health_records_created_on_registration(self) -> None:
        """Gap 5: Health records are persisted when runtimes register."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_hr", "hostname": "host-hr"})

        records = self.client.get("/observability/health-records?entity_id=rtm_hr").json()["data"]["items"]
        self.assertTrue(len(records) >= 1)
        self.assertEqual(records[0]["entity_type"], "runtime")
        self.assertEqual(records[0]["entity_id"], "rtm_hr")
        self.assertEqual(records[0]["health_status"], "healthy")
        self.assertEqual(records[0]["reason"], "registered")

    def test_gap5_health_records_track_degraded_and_offline(self) -> None:
        """Gap 5: Health records track degraded → offline transitions."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_hrt", "hostname": "host-hrt"})
        # Send heartbeat to set last_heartbeat_at
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_hrt")
            # Put heartbeat far enough in past for degraded threshold
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=60)
            session.commit()
        finally:
            session.close()

        # Run sweep with custom thresholds
        session = SessionLocal()
        try:
            sweep_stale_runtimes(session, degraded_timeout_seconds=30, stale_timeout_seconds=120)
        finally:
            session.close()

        records = self.client.get("/observability/health-records?entity_id=rtm_hrt").json()["data"]["items"]
        statuses = [r["health_status"] for r in records]
        self.assertIn("degraded", statuses)

    def test_gap6_capability_pool_created_via_seed_endpoint(self) -> None:
        """Gap 6: POST /capabilities/seed creates both capability and pool."""
        resp = self.client.post("/capabilities/seed", json={
            "capability_id": "cap_seeded",
            "name": "Seeded Cap",
            "version": "v2",
            "image_ref": "img:v2",
            "model_ref": "model:v2",
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertTrue(data["created"])
        self.assertEqual(data["pool_queue_id"], "capability:cap_seeded:v2")
        self.assertEqual(data["pool_routing_policy"], "least_recent")

        # Verify pool is listed
        pools = self.client.get("/capability-pools").json()["data"]["items"]
        seeded = [p for p in pools if p["capability_id"] == "cap_seeded"]
        self.assertEqual(len(seeded), 1)
        self.assertEqual(seeded[0]["queue_id"], "capability:cap_seeded:v2")

    def test_gap6_capability_pool_idempotent_seed(self) -> None:
        """Gap 6: Seeding an existing capability just ensures pool exists."""
        self.client.post("/capabilities/seed", json={
            "capability_id": "cap_idem",
            "name": "Idem",
            "version": "v1",
            "image_ref": "x",
            "model_ref": "y",
        })
        resp2 = self.client.post("/capabilities/seed", json={
            "capability_id": "cap_idem",
            "name": "Idem",
            "version": "v1",
            "image_ref": "x",
            "model_ref": "y",
        })
        self.assertEqual(resp2.status_code, 200)
        self.assertFalse(resp2.json()["data"]["created"])

    def test_gap7_agent_runtime_bindings_written_on_claim(self) -> None:
        """Gap 7: Claiming work writes an agent-runtime binding record."""
        from agp.models import AgentRuntimeBinding
        agent = self.client.post("/agents/up", json={"agent_id": "agt_bind", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_bind", "hostname": "h"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_bind"},
            "message": {"text": "binding work"},
        })
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_bind", "agent_id": "agt_bind", "lease_ttl_seconds": 30,
        })
        self.assertTrue(claim.json()["data"]["claimed"])

        session = SessionLocal()
        try:
            bindings = session.scalars(
                select(AgentRuntimeBinding).where(
                    AgentRuntimeBinding.agent_id == "agt_bind",
                    AgentRuntimeBinding.runtime_id == "rtm_bind",
                )
            ).all()
            self.assertTrue(len(bindings) >= 1)
            active = [b for b in bindings if b.binding_status == "active"]
            self.assertEqual(len(active), 1)
        finally:
            session.close()

    def test_gap7_agent_binding_released_on_lease_expiry(self) -> None:
        """Gap 7: Lease expiry writes a 'released' binding record."""
        from agp.models import AgentRuntimeBinding
        agent = self.client.post("/agents/up", json={"agent_id": "agt_br", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_br", "hostname": "h"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_br"},
            "message": {"text": "expiry work"},
        })
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_br", "agent_id": "agt_br", "lease_ttl_seconds": 1,
        })
        self.assertTrue(claim.json()["data"]["claimed"])

        # Sweep with future time to expire lease
        session = SessionLocal()
        try:
            sweep_expired_leases(session, now=utc_now() + timedelta(seconds=5))
        finally:
            session.close()

        session = SessionLocal()
        try:
            bindings = session.scalars(
                select(AgentRuntimeBinding).where(
                    AgentRuntimeBinding.agent_id == "agt_br",
                    AgentRuntimeBinding.runtime_id == "rtm_br",
                )
            ).all()
            statuses = [b.binding_status for b in bindings]
            self.assertIn("active", statuses)
            self.assertIn("released", statuses)
        finally:
            session.close()

    def test_gap8_runtime_degraded_transition(self) -> None:
        """Gap 8: Runtime transitions to degraded before going offline."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_deg", "hostname": "host-deg"})

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_deg")
            # Put heartbeat past degraded threshold but before offline threshold
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=50)
            session.commit()
        finally:
            session.close()

        session = SessionLocal()
        try:
            result = sweep_stale_runtimes(session, degraded_timeout_seconds=30, stale_timeout_seconds=120)
            self.assertEqual(result["degraded_runtimes"], 1)
            self.assertEqual(result["offline_runtimes"], 0)
        finally:
            session.close()

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_deg")
            self.assertEqual(runtime.status, RuntimeStatus.DEGRADED.value)
            self.assertEqual(runtime.health_status, HealthStatus.DEGRADED.value)
        finally:
            session.close()

    def test_gap8_runtime_degraded_then_offline(self) -> None:
        """Gap 8: Runtime goes degraded, then fully offline on next sweep."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_d2o", "hostname": "host"})

        # Set heartbeat past both thresholds
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_d2o")
            runtime.last_heartbeat_at = utc_now() - timedelta(seconds=200)
            session.commit()
        finally:
            session.close()

        session = SessionLocal()
        try:
            result = sweep_stale_runtimes(session, degraded_timeout_seconds=30, stale_timeout_seconds=90)
            self.assertEqual(result["offline_runtimes"], 1)
        finally:
            session.close()

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_d2o")
            self.assertEqual(runtime.status, RuntimeStatus.OFFLINE.value)
            self.assertEqual(runtime.health_status, HealthStatus.UNREACHABLE.value)
        finally:
            session.close()

    def test_gap8_degraded_runtime_cannot_claim(self) -> None:
        """Gap 8: A degraded runtime cannot claim new work."""
        self.client.post("/agents/up", json={"agent_id": "agt_dcl", "capability_id": "cap_python"})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_dcl", "hostname": "h"})
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_dcl"},
            "message": {"text": "degraded claim test"},
        })

        # Mark runtime as degraded
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_dcl")
            runtime.status = RuntimeStatus.DEGRADED.value
            runtime.health_status = HealthStatus.DEGRADED.value
            session.commit()
        finally:
            session.close()

        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_dcl", "agent_id": "agt_dcl", "lease_ttl_seconds": 30,
        })
        self.assertEqual(claim.status_code, 409)
        self.assertIn("degraded", claim.json()["error"]["message"])

    def test_gap9_operator_triage_endpoint(self) -> None:
        """Gap 9: GET /observability/triage provides consolidated operator view."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_tri", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_tri", "hostname": "host-tri"})
        self.assertEqual(runtime.status_code, 200)

        # Create active work
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_tri"},
            "message": {"text": "triage work"},
        })
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_tri", "agent_id": "agt_tri", "lease_ttl_seconds": 30,
        })
        self.assertTrue(claim.json()["data"]["claimed"])

        triage = self.client.get("/observability/triage").json()["data"]
        self.assertIn("active_jobs_by_runtime", triage)
        self.assertIn("recent_failures", triage)
        self.assertIn("stale_runtimes", triage)
        self.assertIn("capabilities", triage)

        # rtm_tri should have active work
        self.assertIn("rtm_tri", triage["active_jobs_by_runtime"])
        self.assertEqual(len(triage["active_jobs_by_runtime"]["rtm_tri"]), 1)

        # Capabilities should list cap_python
        cap_ids = [c["capability_id"] for c in triage["capabilities"]]
        self.assertIn("cap_python", cap_ids)

    def test_gap10_duplicate_claim_prevention(self) -> None:
        """Gap 10: A job cannot be claimed twice simultaneously."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_dup", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime1 = self.client.post("/runtimes/register", json={"runtime_id": "rtm_dup1", "hostname": "h1"})
        self.assertEqual(runtime1.status_code, 200)
        runtime2 = self.client.post("/runtimes/register", json={"runtime_id": "rtm_dup2", "hostname": "h2"})
        self.assertEqual(runtime2.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_dup"},
            "message": {"text": "duplicate prevention test"},
        })

        # First claim succeeds
        claim1 = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_dup1", "agent_id": "agt_dup", "lease_ttl_seconds": 30,
        })
        self.assertTrue(claim1.json()["data"]["claimed"])

        # Second claim for same agent must not claim (agent is busy)
        claim2 = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_dup2", "agent_id": "agt_dup", "lease_ttl_seconds": 30,
        })
        self.assertFalse(claim2.json()["data"]["claimed"])

    def test_gap10_fencing_token_rejects_stale_terminal(self) -> None:
        """Gap 10: Expired lease's fencing token must be rejected for terminal operations."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_fence", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_fence", "hostname": "h"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_fence"},
            "message": {"text": "fence test"},
        })

        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_fence", "agent_id": "agt_fence", "lease_ttl_seconds": 1,
        })
        first = claim.json()["data"]
        self.assertTrue(first["claimed"])

        # Expire the lease
        session = SessionLocal()
        try:
            sweep_expired_leases(session, now=utc_now() + timedelta(seconds=5))
        finally:
            session.close()

        # Try to complete with the old fencing token — must be rejected
        artifacts = self._materialize_terminal_artifacts({
            "prompt.txt": "prompt",
            "transcript.txt": "transcript_log",
            "exec.txt": "exec_log",
            "result.txt": "result",
        })
        complete = self.client.post(f"/runs/{first['run']['run_id']}/complete", json={
            "runtime_id": "rtm_fence",
            "lease_id": first["lease"]["lease_id"],
            "fencing_token": first["lease"]["fencing_token"],
            "artifacts": artifacts,
        })
        self.assertEqual(complete.status_code, 409)

    def test_gap10_network_partition_reconnect(self) -> None:
        """Gap 10: Runtime goes offline then re-registers; agent can be reclaimed."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_part", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_part", "hostname": "h"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_part"},
            "message": {"text": "partition test"},
        })
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_part", "agent_id": "agt_part", "lease_ttl_seconds": 1,
        })
        self.assertTrue(claim.json()["data"]["claimed"])

        # Simulate partition: expire lease, mark runtime offline
        session = SessionLocal()
        try:
            sweep_expired_leases(session, now=utc_now() + timedelta(seconds=5))
            runtime_row = session.get(Runtime, "rtm_part")
            runtime_row.last_heartbeat_at = utc_now() - timedelta(seconds=200)
            session.commit()
            sweep_stale_runtimes(session, stale_timeout_seconds=90)
        finally:
            session.close()

        # Re-register runtime (reconnect)
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_part", "hostname": "h"})

        # Agent should be idle and reclaimable (job was requeued)
        session = SessionLocal()
        try:
            agent_row = session.get(Agent, "agt_part")
            self.assertEqual(agent_row.status, "idle")
        finally:
            session.close()

    def test_gap10_lease_expiry_and_reassignment(self) -> None:
        """Gap 10: After lease expiry, job is reassigned with a new fencing token."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_reass", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_reass", "hostname": "h"})
        self.assertEqual(runtime.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_reass"},
            "message": {"text": "reassignment test"},
        })

        # First claim
        claim1 = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_reass", "agent_id": "agt_reass", "lease_ttl_seconds": 1,
        })
        first = claim1.json()["data"]
        self.assertTrue(first["claimed"])
        first_fencing = first["lease"]["fencing_token"]

        # Expire lease and reconstruct queue for requeued job
        session = SessionLocal()
        try:
            sweep_expired_leases(session, now=utc_now() + timedelta(seconds=5))
        finally:
            session.close()
        from agp._ops_helpers import reconstruct_queue_from_state
        reconstruct_queue_from_state()

        # Re-register runtime and reclaim
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_reass", "hostname": "h"})
        claim2 = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_reass", "agent_id": "agt_reass", "lease_ttl_seconds": 30,
        })
        second = claim2.json()["data"]
        self.assertTrue(second["claimed"])
        second_fencing = second["lease"]["fencing_token"]

        # New fencing token must be different (higher)
        self.assertGreater(second_fencing, first_fencing)

    def test_gap10_agent_runtime_replacement(self) -> None:
        """Gap 10: Agent survives runtime replacement and continues processing."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_rep", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime1 = self.client.post("/runtimes/register", json={"runtime_id": "rtm_rep1", "hostname": "h1"})
        self.assertEqual(runtime1.status_code, 200)

        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_rep"},
            "message": {"text": "replacement test"},
        })

        # Claim on runtime1
        claim1 = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_rep1", "agent_id": "agt_rep", "lease_ttl_seconds": 1,
        })
        self.assertTrue(claim1.json()["data"]["claimed"])

        # Runtime1 dies — expire lease and reconstruct queue
        session = SessionLocal()
        try:
            sweep_expired_leases(session, now=utc_now() + timedelta(seconds=5))
        finally:
            session.close()
        from agp._ops_helpers import reconstruct_queue_from_state
        reconstruct_queue_from_state()

        # Runtime2 registers and claims the same agent
        runtime2 = self.client.post("/runtimes/register", json={"runtime_id": "rtm_rep2", "hostname": "h2"})
        self.assertEqual(runtime2.status_code, 200)

        claim2 = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_rep2", "agent_id": "agt_rep", "lease_ttl_seconds": 30,
        })
        second = claim2.json()["data"]
        self.assertTrue(second["claimed"])

        # Complete on runtime2
        artifacts = self._materialize_terminal_artifacts({
            "prompt.txt": "prompt",
            "transcript.txt": "transcript_log",
            "exec.txt": "exec_log",
            "result.txt": "result",
        })
        complete = self.client.post(f"/runs/{second['run']['run_id']}/complete", json={
            "runtime_id": "rtm_rep2",
            "lease_id": second["lease"]["lease_id"],
            "fencing_token": second["lease"]["fencing_token"],
            "artifacts": artifacts,
        })
        self.assertEqual(complete.status_code, 200)

        # Job should be completed
        job = self.client.get(f"/jobs/{second['job']['job_id']}").json()["data"]
        self.assertEqual(job["status"], "completed")

    def test_gap10_operator_inspection_depth(self) -> None:
        """Gap 10: Operator APIs return sufficient detail for triage."""
        agent = self.client.post("/agents/up", json={"agent_id": "agt_ins", "capability_id": "cap_python"})
        self.assertEqual(agent.status_code, 200)
        runtime = self.client.post("/runtimes/register", json={"runtime_id": "rtm_ins", "hostname": "host-ins"})
        self.assertEqual(runtime.status_code, 200)

        sent = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_ins"},
            "message": {"text": "inspection test"},
        })
        job_id = sent.json()["data"]["job_id"]

        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_ins", "agent_id": "agt_ins", "lease_ttl_seconds": 30,
        })
        data = claim.json()["data"]
        run_id = data["run"]["run_id"]

        # Trace must include timeline and durations
        trace = self.client.get(f"/observability/jobs/{job_id}/trace").json()["data"]
        self.assertIn("timeline", trace)
        self.assertIn("trace", trace)
        self.assertIn("runs", trace)
        self.assertIn("durations_seconds", trace["trace"])

        # Summary must include counts for all key states
        summary = self.client.get("/observability/summary").json()["data"]
        self.assertIn("jobs", summary)
        self.assertIn("runtimes", summary)
        self.assertIn("agents", summary)
        self.assertIn("leases", summary)
        self.assertIn("queue", summary)

    def test_agp_up_provisions_agent_from_capability_name(self) -> None:
        result = self._cli_invoke(["up", "Python Tester"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[SUCCESS]", result.output)
        self.assertIn("AGENT_ID:", result.output)
        self.assertIn("STATUS:     IDLE", result.output)
        self.assertIn("CAPABILITY: Python Tester", result.output)

    def test_agp_up_auto_generates_agent_id(self) -> None:
        result = self._cli_invoke(["up", "Python Tester"])
        self.assertEqual(result.exit_code, 0, result.output)
        for line in result.output.splitlines():
            if line.startswith("AGENT_ID:"):
                agent_id = line.split(":")[1].strip()
                self.assertTrue(agent_id.startswith("agt_"), f"Expected agt_ prefix, got {agent_id}")
                break
        else:
            self.fail("AGENT_ID not found in output")

    def test_agp_up_unknown_capability_fails(self) -> None:
        result = self._cli_invoke(["up", "nonexistent"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("[ERROR]", result.output)
        self.assertIn("Unknown capability", result.output)

    def test_agp_up_duplicate_agent_id_fails(self) -> None:
        self.client.post("/agents/up", json={
            "agent_id": "agt_dup", "capability_id": "cap_python",
        })
        result = self._cli_invoke(["up", "Python Tester", "--agent-id", "agt_dup"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("[ERROR]", result.output)
        self.assertIn("already exists", result.output)

    def test_agp_down_idle_agent(self) -> None:
        self.client.post("/agents/up", json={
            "agent_id": "agt_down_idle", "capability_id": "cap_python",
        })
        result = self._cli_invoke(["down", "agt_down_idle"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[SUCCESS]", result.output)
        self.assertIn("AGENT_ID:   agt_down_idle", result.output)

    def test_agp_down_busy_agent_without_force_blocked(self) -> None:
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_dwn", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_down_busy", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_dwn",
        })
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_down_busy"},
            "message": {"text": "work"},
        })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_dwn", "agent_id": "agt_down_busy",
        })
        result = self._cli_invoke(["down", "agt_down_busy"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("BUSY", result.output)
        self.assertIn("--force", result.output)

    def test_agp_down_busy_agent_with_force_cancels_jobs_runs_leases(self) -> None:
        """Force-down cancels jobs AND their runs and leases."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_dwn2", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_down_force", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_dwn2",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_down_force"},
            "message": {"text": "work"},
        })
        job_id = send_resp.json()["data"]["job_id"]
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_dwn2", "agent_id": "agt_down_force",
        })
        run_id = claim.json()["data"]["run"]["run_id"]
        lease_id = claim.json()["data"]["lease"]["lease_id"]

        result = self._cli_invoke(["down", "agt_down_force", "--force"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("[SUCCESS]", result.output)
        self.assertIn("Forcefully", result.output)

        # Verify job, run, and lease all cancelled/released
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "cancelled")

        session = SessionLocal()
        try:
            run = session.get(Run, run_id)
            self.assertEqual(run.status, "cancelled")
            lease = session.get(Lease, lease_id)
            self.assertEqual(lease.status, "released")
        finally:
            session.close()

    def test_agp_down_nonexistent_agent_fails(self) -> None:
        result = self._cli_invoke(["down", "agt_ghost"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("[ERROR]", result.output)
        self.assertIn("not found", result.output)

    def test_agp_down_already_terminated_fails(self) -> None:
        self.client.post("/agents/up", json={
            "agent_id": "agt_term", "capability_id": "cap_python",
        })
        self._cli_invoke(["down", "agt_term"])
        result = self._cli_invoke(["down", "agt_term"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("already terminated", result.output)

    def test_agp_down_draining_without_force_blocked(self) -> None:
        self.client.post("/agents/up", json={
            "agent_id": "agt_drain", "capability_id": "cap_python",
        })
        self.client.post("/agents/agt_drain/down", json={"mode": "drain"})
        result = self._cli_invoke(["down", "agt_drain"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("DRAINING", result.output)
        self.assertIn("--force", result.output)

    def test_agp_down_force_with_multiple_jobs(self) -> None:
        """Force-down cancels ALL active jobs, not just the first."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_multi", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_multi", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_multi",
        })
        j1 = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_multi"},
            "message": {"text": "job1"},
        }).json()["data"]["job_id"]
        j2 = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_multi"},
            "message": {"text": "job2"},
        }).json()["data"]["job_id"]
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_multi", "agent_id": "agt_multi",
        })

        result = self._cli_invoke(["down", "agt_multi", "--force"])
        self.assertEqual(result.exit_code, 0, result.output)

        self.assertEqual(self.client.get(f"/jobs/{j1}").json()["data"]["status"], "cancelled")
        self.assertEqual(self.client.get(f"/jobs/{j2}").json()["data"]["status"], "cancelled")

    def test_agent_down_service_toctou_guard_busy(self) -> None:
        """Server rejects terminate on busy agent."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_toctou", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_toctou", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_toctou",
        })
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_toctou"},
            "message": {"text": "work"},
        })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_toctou", "agent_id": "agt_toctou",
        })
        resp = self.client.post("/agents/agt_toctou/down", json={"mode": "terminate"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("active work", resp.json()["error"]["message"])

    def test_agent_down_service_toctou_guard_draining(self) -> None:
        """Server rejects terminate on draining agent (may have active work)."""
        self.client.post("/agents/up", json={
            "agent_id": "agt_drain_guard", "capability_id": "cap_python",
        })
        self.client.post("/agents/agt_drain_guard/down", json={"mode": "drain"})
        resp = self.client.post("/agents/agt_drain_guard/down", json={"mode": "terminate"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("active work", resp.json()["error"]["message"])

    def test_agent_down_service_double_drain_rejected(self) -> None:
        """Server rejects drain on already-draining agent."""
        self.client.post("/agents/up", json={
            "agent_id": "agt_dd", "capability_id": "cap_python",
        })
        self.client.post("/agents/agt_dd/down", json={"mode": "drain"})
        resp = self.client.post("/agents/agt_dd/down", json={"mode": "drain"})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already draining", resp.json()["error"]["message"])

    def test_agp_down_force_draining_agent_with_active_work(self) -> None:
        """Force-down on a draining agent with active jobs cancels everything."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_drnf", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_drnf", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_drnf",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_drnf"},
            "message": {"text": "work"},
        })
        job_id = send_resp.json()["data"]["job_id"]
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_drnf", "agent_id": "agt_drnf",
        })
        run_id = claim.json()["data"]["run"]["run_id"]
        lease_id = claim.json()["data"]["lease"]["lease_id"]

        # Put into draining first — agent still has active work
        self.client.post("/agents/agt_drnf/down", json={"mode": "drain"})

        # Force-down should cancel everything
        result = self._cli_invoke(["down", "agt_drnf", "--force"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Forcefully", result.output)

        # Verify all cancelled/released
        self.assertEqual(self.client.get(f"/jobs/{job_id}").json()["data"]["status"], "cancelled")
        session = SessionLocal()
        try:
            self.assertEqual(session.get(Run, run_id).status, "cancelled")
            self.assertEqual(session.get(Lease, lease_id).status, "released")
        finally:
            session.close()

    def test_drain_preserved_after_run_completes(self) -> None:
        """Completing a run on a draining agent preserves DRAINING, not IDLE."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_drn", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_drn_pres", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_drn",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_drn_pres"},
            "message": {"text": "work"},
        })
        job_id = send_resp.json()["data"]["job_id"]
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_drn", "agent_id": "agt_drn_pres",
        })
        run_id = claim.json()["data"]["run"]["run_id"]
        lease_id = claim.json()["data"]["lease"]["lease_id"]
        fencing = claim.json()["data"]["lease"]["fencing_token"]

        # Drain while run is active
        self.client.post("/agents/agt_drn_pres/down", json={"mode": "drain"})

        # Heartbeat to promote leased → running
        self.client.post(f"/runs/{run_id}/heartbeat", json={
            "runtime_id": "rtm_drn", "lease_id": lease_id, "fencing_token": fencing,
        })

        # Complete the run
        self.client.post(f"/runs/{run_id}/complete", json={
            "runtime_id": "rtm_drn", "lease_id": lease_id,
            "fencing_token": fencing, "artifacts": [], "summary": {},
        })

        # Agent should still be DRAINING, not reverted to IDLE
        agent = self.client.get("/agents/agt_drn_pres").json()["data"]
        self.assertEqual(agent["status"], "draining")

    def test_force_down_transitions_runtime_to_idle(self) -> None:
        """Force-down releases all leases and transitions the runtime back to IDLE."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_rtm_idle", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_rtm_idle", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_rtm_idle",
        })
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_rtm_idle"},
            "message": {"text": "work"},
        })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_rtm_idle", "agent_id": "agt_rtm_idle",
        })

        # Runtime should be busy
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_rtm_idle")
            self.assertEqual(runtime.status, "busy")
        finally:
            session.close()

        # Force-down the agent
        self._cli_invoke(["down", "agt_rtm_idle", "--force"])

        # Runtime should be back to idle
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_rtm_idle")
            self.assertEqual(runtime.status, "idle")
        finally:
            session.close()

    def test_force_down_event_records_previous_status(self) -> None:
        """Events from force-down include previous_status for audit trail."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_evt", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_evt", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_evt",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_evt"},
            "message": {"text": "work"},
        })
        job_id = send_resp.json()["data"]["job_id"]
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_evt", "agent_id": "agt_evt",
        })

        self.client.post("/agents/agt_evt/down", json={"mode": "force"})

        # Check events for previous_status
        events_resp = self.client.get(f"/jobs/{job_id}/events").json()["data"]["items"]
        cancelled_events = [e for e in events_resp if e["event_type"] == "job.cancelled"]
        self.assertTrue(len(cancelled_events) >= 1)
        self.assertEqual(cancelled_events[0]["body"]["previous_status"], "running")

        run_cancelled = [e for e in events_resp if e["event_type"] == "run.cancelled"]
        self.assertTrue(len(run_cancelled) >= 1)
        self.assertIn("previous_status", run_cancelled[0]["body"])

        lease_released = [e for e in events_resp if e["event_type"] == "lease.released"]
        self.assertTrue(len(lease_released) >= 1)
        self.assertIn("previous_status", lease_released[0]["body"])

    # ── agp interrupt tests ──────────────────────────────────────────

    def test_agent_interrupt_cancels_active_job(self) -> None:
        """Scenario A: interrupt an agent with an active running job."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_int", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_int", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_int",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_int"},
            "message": {"text": "active work"},
        })
        job_id = send_resp.json()["data"]["job_id"]
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_int", "agent_id": "agt_int",
        })

        resp = self.client.post("/agents/agt_int/interrupt", json={"purge": False})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["agent_id"], "agt_int")
        self.assertEqual(data["halted_job_id"], job_id)
        self.assertEqual(data["dropped_job_ids"], [])
        self.assertEqual(data["status"], "idle")

        # Job should be cancelled
        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "cancelled")

    def test_agent_interrupt_with_purge_empties_queue(self) -> None:
        """Scenario B: interrupt with --purge cancels active + all queued."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_purge", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_purge", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_purge",
        })
        # Send 3 jobs — first will be claimed, other 2 stay queued
        job_ids = []
        for i in range(3):
            r = self.client.post("/messages/send", json={
                "target": {"type": "agent", "id": "agt_purge"},
                "message": {"text": f"task {i}"},
            })
            job_ids.append(r.json()["data"]["job_id"])
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_purge", "agent_id": "agt_purge",
        })

        resp = self.client.post("/agents/agt_purge/interrupt", json={"purge": True})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["halted_job_id"], job_ids[0])
        self.assertEqual(len(data["dropped_job_ids"]), 2)
        self.assertIn(job_ids[1], data["dropped_job_ids"])
        self.assertIn(job_ids[2], data["dropped_job_ids"])
        self.assertEqual(data["remaining_queue_size"], 0)

        # All jobs should be cancelled
        for jid in job_ids:
            job = self.client.get(f"/jobs/{jid}").json()["data"]
            self.assertEqual(job["status"], "cancelled")

    def test_agent_interrupt_no_active_job_is_noop(self) -> None:
        """Interrupt on idle agent with no active job succeeds gracefully."""
        self.client.post("/agents/up", json={
            "agent_id": "agt_idle_int", "capability_id": "cap_python",
        })
        resp = self.client.post("/agents/agt_idle_int/interrupt")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIsNone(data["halted_job_id"])
        self.assertEqual(data["dropped_job_ids"], [])
        self.assertEqual(data["status"], "idle")

    def test_agent_interrupt_terminated_agent_rejected(self) -> None:
        """Cannot interrupt a terminated agent."""
        self.client.post("/agents/up", json={
            "agent_id": "agt_term_int", "capability_id": "cap_python",
        })
        self.client.post("/agents/agt_term_int/down", json={"mode": "terminate"})

        resp = self.client.post("/agents/agt_term_int/interrupt")
        self.assertEqual(resp.status_code, 409)

    def test_agent_interrupt_releases_lease_and_idles_runtime(self) -> None:
        """Interrupt releases the active lease and transitions runtime to IDLE."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_int_rt", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_int_rt", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_int_rt",
        })
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_int_rt"},
            "message": {"text": "work"},
        })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_int_rt", "agent_id": "agt_int_rt",
        })
        # Runtime should be busy after claim
        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_int_rt")
            self.assertEqual(runtime.status, "busy")
        finally:
            session.close()

        self.client.post("/agents/agt_int_rt/interrupt")

        session = SessionLocal()
        try:
            runtime = session.get(Runtime, "rtm_int_rt")
            self.assertEqual(runtime.status, "idle")
            # Lease should be released
            lease = session.scalars(
                select(Lease).where(Lease.runtime_id == "rtm_int_rt")
            ).first()
            self.assertEqual(lease.status, "released")
        finally:
            session.close()

    def test_agent_interrupt_preserves_draining_status(self) -> None:
        """Interrupt on a draining agent keeps DRAINING status, doesn't reset to IDLE."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_drain_int", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_drain_int", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_drain_int",
        })
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_drain_int"},
            "message": {"text": "work"},
        })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_drain_int", "agent_id": "agt_drain_int",
        })
        # Put agent in draining
        self.client.post("/agents/agt_drain_int/down", json={"mode": "drain"})

        resp = self.client.post("/agents/agt_drain_int/interrupt")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "draining")

    def test_agent_interrupt_event_audit_trail(self) -> None:
        """Interrupt creates agent.interrupted event with correct body."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_evt_int", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_evt_int", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_evt_int",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_evt_int"},
            "message": {"text": "work"},
        })
        job_id = send_resp.json()["data"]["job_id"]
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_evt_int", "agent_id": "agt_evt_int",
        })

        self.client.post("/agents/agt_evt_int/interrupt", json={"purge": True})

        # Check for agent.interrupted event
        session = SessionLocal()
        try:
            events = session.scalars(
                select(Event).where(Event.event_type == "agent.interrupted")
            ).all()
            int_events = [e for e in events if e.body_json.get("halted_job_id") == job_id]
            self.assertEqual(len(int_events), 1)
            body = int_events[0].body_json
            self.assertEqual(body["halted_job_id"], job_id)
            self.assertTrue(body["purge"])
        finally:
            session.close()

    def test_job_interrupt_queued_job_via_api(self) -> None:
        """Scenario C: interrupting a queued (not yet running) job cancels it directly."""
        self.client.post("/agents/up", json={
            "agent_id": "agt_jint", "capability_id": "cap_python",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_jint"},
            "message": {"text": "queued work"},
        })
        job_id = send_resp.json()["data"]["job_id"]

        resp = self.client.post(f"/jobs/{job_id}/interrupt")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["status"], "cancelled")

    def test_cli_interrupt_agent_happy_path(self) -> None:
        """CLI: agp interrupt <agent_id> produces correct output."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_cli_int", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_cli_int", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_cli_int",
        })
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_cli_int"},
            "message": {"text": "work"},
        })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_cli_int", "agent_id": "agt_cli_int",
        })

        result = self._cli_invoke(["interrupt", "agt_cli_int"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Execution Interrupted", result.output)
        self.assertIn("HALTED JOB:", result.output)
        self.assertIn("agt_cli_int", result.output)

    def test_cli_interrupt_agent_with_purge(self) -> None:
        """CLI: agp interrupt <agent_id> --purge shows dropped jobs."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_cli_purge", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_cli_purge", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_cli_purge",
        })
        for i in range(3):
            self.client.post("/messages/send", json={
                "target": {"type": "agent", "id": "agt_cli_purge"},
                "message": {"text": f"task {i}"},
            })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_cli_purge", "agent_id": "agt_cli_purge",
        })

        result = self._cli_invoke(["interrupt", "agt_cli_purge", "--purge"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Agent Purged and Reset", result.output)
        self.assertIn("DROPPED JOBS:", result.output)
        self.assertIn("Purging", result.output)

    def test_cli_interrupt_job_target(self) -> None:
        """CLI: agp interrupt <job_id> cancels queued job with correct output."""
        self.client.post("/agents/up", json={
            "agent_id": "agt_cli_jint", "capability_id": "cap_python",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_cli_jint"},
            "message": {"text": "queued task"},
        })
        job_id = send_resp.json()["data"]["job_id"]

        result = self._cli_invoke(["interrupt", job_id])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Job Removed from Queue", result.output)
        self.assertIn(job_id, result.output)
        self.assertIn("not yet started execution", result.output)

    def test_cli_interrupt_nonexistent_target(self) -> None:
        """CLI: agp interrupt <nonexistent> fails with clear error."""
        result = self._cli_invoke(["interrupt", "nonexistent_thing"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("not found", result.output.lower())

    def test_agent_interrupt_purge_no_active_job_clears_queue(self) -> None:
        """Purge with no active job but queued jobs cancels the backlog."""
        self.client.post("/agents/up", json={
            "agent_id": "agt_purge_idle", "capability_id": "cap_python",
        })
        job_ids = []
        for i in range(3):
            r = self.client.post("/messages/send", json={
                "target": {"type": "agent", "id": "agt_purge_idle"},
                "message": {"text": f"task {i}"},
            })
            job_ids.append(r.json()["data"]["job_id"])

        resp = self.client.post("/agents/agt_purge_idle/interrupt", json={"purge": True})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertIsNone(data["halted_job_id"])
        self.assertEqual(len(data["dropped_job_ids"]), 3)
        self.assertEqual(data["remaining_queue_size"], 0)
        for jid in job_ids:
            job = self.client.get(f"/jobs/{jid}").json()["data"]
            self.assertEqual(job["status"], "cancelled")

    def test_agent_interrupt_capability_routed_active_job(self) -> None:
        """Interrupt finds and cancels a capability-routed job via Run join."""
        from agp.db import SessionLocal
        from agp.models import Capability, utc_now as _utc_now
        session = SessionLocal()
        try:
            session.add(Capability(
                capability_id="cap_route_int",
                name="Route Int Tester",
                version="v1",
                image_ref="python:3.12",
                model_ref="gpt-5.4",
                resource_tier="small",
                permission_profile="default",
                queue_mode="capability_pool",
                runtime_requirements_json={},
                created_at=_utc_now(),
                updated_at=_utc_now(),
            ))
            session.commit()
        finally:
            session.close()

        self.client.post("/runtimes/register", json={"runtime_id": "rtm_route_int", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_route_int", "capability_id": "cap_route_int",
            "assigned_runtime_id": "rtm_route_int",
        })
        send_resp = self.client.post("/messages/send", json={
            "target": {"type": "capability", "id": "cap_route_int"},
            "message": {"text": "capability work"},
        })
        job_id = send_resp.json()["data"]["job_id"]
        # Job is capability-routed: target_agent_id is NULL
        job_data = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertIsNone(job_data["target_agent_id"])

        # Claim it via the capability pool
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_route_int", "agent_id": "agt_route_int",
            "capability_id": "cap_route_int",
        })

        # Interrupt the agent — should find the capability-routed job via Path 2
        resp = self.client.post("/agents/agt_route_int/interrupt")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["halted_job_id"], job_id)

        job = self.client.get(f"/jobs/{job_id}").json()["data"]
        self.assertEqual(job["status"], "cancelled")

    def test_agent_interrupt_double_is_idempotent(self) -> None:
        """Calling interrupt twice is a no-op on the second call."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_dbl_int", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_dbl_int", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_dbl_int",
        })
        self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_dbl_int"},
            "message": {"text": "work"},
        })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_dbl_int", "agent_id": "agt_dbl_int",
        })

        resp1 = self.client.post("/agents/agt_dbl_int/interrupt")
        self.assertEqual(resp1.status_code, 200)
        self.assertIsNotNone(resp1.json()["data"]["halted_job_id"])

        resp2 = self.client.post("/agents/agt_dbl_int/interrupt")
        self.assertEqual(resp2.status_code, 200)
        self.assertIsNone(resp2.json()["data"]["halted_job_id"])
        self.assertEqual(resp2.json()["data"]["status"], "idle")

    def test_agent_interrupt_remaining_queue_count(self) -> None:
        """Interrupt without purge reports correct remaining queue size."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_rem", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_rem", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_rem",
        })
        for i in range(4):
            self.client.post("/messages/send", json={
                "target": {"type": "agent", "id": "agt_rem"},
                "message": {"text": f"task {i}"},
            })
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_rem", "agent_id": "agt_rem",
        })

        resp = self.client.post("/agents/agt_rem/interrupt", json={"purge": False})
        data = resp.json()["data"]
        self.assertEqual(data["remaining_queue_size"], 3)
        self.assertEqual(data["status"], "idle")

    def test_agent_interrupt_delivery_records_cleaned(self) -> None:
        """Interrupt acks dangling delivery records for cancelled jobs."""
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_dlv", "hostname": "localhost"})
        self.client.post("/agents/up", json={
            "agent_id": "agt_dlv", "capability_id": "cap_python",
            "assigned_runtime_id": "rtm_dlv",
        })
        job_ids = []
        for i in range(3):
            r = self.client.post("/messages/send", json={
                "target": {"type": "agent", "id": "agt_dlv"},
                "message": {"text": f"task {i}"},
            })
            job_ids.append(r.json()["data"]["job_id"])
        self.client.post("/runs/claim", json={
            "runtime_id": "rtm_dlv", "agent_id": "agt_dlv",
        })

        self.client.post("/agents/agt_dlv/interrupt", json={"purge": True})

        session = SessionLocal()
        try:
            for jid in job_ids:
                deliveries = session.scalars(
                    select(QueueDeliveryRecord).where(
                        QueueDeliveryRecord.job_id == jid,
                    )
                ).all()
                for d in deliveries:
                    self.assertNotIn(d.state, ("pending", "delivered"),
                                     f"Delivery for {jid} still in {d.state}")
        finally:
            session.close()
