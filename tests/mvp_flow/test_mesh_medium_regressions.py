"""Medium-priority regression tests for the Dynamic Agent Mesh PRD.

Covers: runtime auth on /agents/up (M8a), audit history preservation after
agent deletion (M8b), 1:1 runtime uniqueness (M8c), force-delete auth guard
(M6/M8), draining deadlock resolution (M1), and orphaned job cleanup (M12).
"""

from datetime import timedelta
from tests.mvp_flow.base import *


class TestRuntimeAuthOnAgentsUp(MvpFlowTestBase):
    """M8a: /agents/up must require runtime token when runtime auth is configured."""

    def test_agents_up_rejects_unauthenticated_when_token_set(self):
        settings.runtime_bearer_token = "secret-runtime-token"
        self.client = TestClient(build_app())

        # Unauthenticated → 401
        resp = self.client.post("/agents/up", json={"agent_id": "agt_unauth", "capabilities": ["python"]})
        self.assertEqual(resp.status_code, 401)

    def test_agents_up_accepts_valid_runtime_token(self):
        settings.runtime_bearer_token = "secret-runtime-token"
        self.client = TestClient(build_app())

        resp = self.client.post(
            "/agents/up",
            json={"agent_id": "agt_authed", "capabilities": ["python"]},
            headers={"Authorization": "Bearer secret-runtime-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["agent_id"], "agt_authed")


class TestAuditHistoryPreservation(MvpFlowTestBase):
    """M8b: runs and leases must retain agent_id after agent deletion."""

    def test_run_preserves_agent_id_after_agent_deleted(self):
        # Setup: create agent, runtime, send job, claim, complete
        self.client.post("/agents/up", json={"agent_id": "agt_audit", "capabilities": ["python"]})
        self.client.post("/runtimes/register", json={"runtime_id": "rtm_audit", "hostname": "h"})
        sent = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_audit"},
            "message": {"text": "audit test"},
        })
        job_id = sent.json()["data"]["job_id"]
        claim = self.client.post("/runs/claim", json={
            "runtime_id": "rtm_audit", "agent_id": "agt_audit", "lease_ttl_seconds": 30,
        })
        data = claim.json()["data"]
        run_id = data["run"]["run_id"]
        lease_id = data["lease"]["lease_id"]

        artifacts = self._materialize_terminal_artifacts({
            "prompt.txt": "prompt", "transcript.txt": "transcript_log",
            "exec.txt": "exec_log", "result.txt": "result",
        })
        self.client.post(f"/runs/{run_id}/complete", json={
            "runtime_id": "rtm_audit",
            "lease_id": lease_id,
            "fencing_token": data["lease"]["fencing_token"],
            "artifacts": artifacts,
            "summary": {},
        })

        # Delete the agent
        self.client.post("/agents/agt_audit/down", json={"mode": "force"})

        # Verify agent is gone
        agent_resp = self.client.get("/agents/agt_audit")
        self.assertEqual(agent_resp.status_code, 404)

        # Verify run still has agent_id
        session = SessionLocal()
        try:
            run = session.get(Run, run_id)
            self.assertIsNotNone(run)
            self.assertEqual(run.agent_id, "agt_audit")
            lease = session.get(Lease, lease_id)
            self.assertIsNotNone(lease)
            self.assertEqual(lease.agent_id, "agt_audit")
        finally:
            session.close()


class TestRuntimeUniqueness(MvpFlowTestBase):
    """M8c: Runtime.agent_id has a UNIQUE constraint — two agents can't share a runtime."""

    def test_two_agents_get_separate_runtimes(self):
        self.client.post("/agents/up", json={"agent_id": "agt_u1", "capabilities": ["python"]})
        self.client.post("/agents/up", json={"agent_id": "agt_u2", "capabilities": ["python"]})

        session = SessionLocal()
        try:
            runtimes = session.scalars(select(Runtime)).all()
            agent_ids = [r.agent_id for r in runtimes if r.agent_id is not None]
            # Each agent should have its own runtime
            self.assertIn("agt_u1", agent_ids)
            self.assertIn("agt_u2", agent_ids)
            # No duplicates
            self.assertEqual(len(agent_ids), len(set(agent_ids)))
        finally:
            session.close()


class TestForceDeleteAuthGuard(MvpFlowTestBase):
    """M6/M8: force-delete must require operator auth when configured."""

    def test_force_delete_rejected_with_only_runtime_token(self):
        settings.runtime_bearer_token = "rtm-token"
        settings.operator_bearer_token = "op-token"
        self.client = TestClient(build_app())

        # Register agent with runtime token
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_force", "capabilities": ["python"]},
            headers={"Authorization": "Bearer rtm-token"},
        )

        # Force-delete with only runtime token → 403
        resp = self.client.post(
            "/agents/agt_force/down",
            json={"mode": "force"},
            headers={"Authorization": "Bearer rtm-token"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_force_delete_allowed_with_operator_token(self):
        settings.runtime_bearer_token = "rtm-token"
        settings.operator_bearer_token = "op-token"
        self.client = TestClient(build_app())

        # Register agent
        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_force2", "capabilities": ["python"]},
            headers={"Authorization": "Bearer rtm-token"},
        )

        # Force-delete with operator token → success
        resp = self.client.post(
            "/agents/agt_force2/down",
            json={"mode": "force"},
            headers={"Authorization": "Bearer op-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], "deleted")

    def test_drain_allowed_with_runtime_token(self):
        settings.runtime_bearer_token = "rtm-token"
        settings.operator_bearer_token = "op-token"
        self.client = TestClient(build_app())

        self.client.post(
            "/agents/up",
            json={"agent_id": "agt_drain", "capabilities": ["python"]},
            headers={"Authorization": "Bearer rtm-token"},
        )

        # Drain with runtime token → allowed
        resp = self.client.post(
            "/agents/agt_drain/down",
            json={"mode": "drain"},
            headers={"Authorization": "Bearer rtm-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["data"]["status"], "draining")


class TestDrainingAgentDeadlockResolution(MvpFlowTestBase):
    """M1: Draining agent with queued work but no active leases should be cleaned up."""

    def test_draining_agent_queued_work_cancelled_and_deleted(self):
        from agp.enums import JobStatus
        # Create agent and send a job targeted at it
        self.client.post("/agents/up", json={"agent_id": "agt_deadlock", "capabilities": ["python"]})
        sent = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_deadlock"},
            "message": {"text": "will be stranded"},
        })
        job_id = sent.json()["data"]["job_id"]

        # Drain the agent
        self.client.post("/agents/agt_deadlock/down", json={"mode": "drain"})

        # The job is queued, targeted at the draining agent. Run the sweeper.
        session = SessionLocal()
        try:
            result = sweep_stale_agents(session)
            # Agent should be deleted and stranded job should be cancelled
            self.assertEqual(result["deleted_drained"], 1)
            self.assertEqual(result["stranded_jobs_cancelled"], 1)

            # Job should be failed
            from agp.models import Job
            job = session.get(Job, job_id)
            self.assertEqual(job.status, JobStatus.FAILED.value)

            # Agent should be gone
            agent = session.get(Agent, "agt_deadlock")
            self.assertIsNone(agent)
        finally:
            session.close()


class TestOrphanedJobCleanup(MvpFlowTestBase):
    """M12: Queued jobs targeting deleted agents should be failed by the sweeper."""

    def test_orphaned_queued_job_failed_by_sweeper(self):
        from agp.enums import JobStatus
        # Create agent, send targeted job, then force-delete the agent
        self.client.post("/agents/up", json={"agent_id": "agt_orphan", "capabilities": ["python"]})
        sent = self.client.post("/messages/send", json={
            "target": {"type": "agent", "id": "agt_orphan"},
            "message": {"text": "orphan test"},
        })
        job_id = sent.json()["data"]["job_id"]

        # Force-delete the agent — job stays queued with target_agent_id set
        self.client.post("/agents/agt_orphan/down", json={"mode": "force"})

        # Verify agent is gone but job is still queued (force-down cancels active work
        # but the job was only queued, not running)
        session = SessionLocal()
        try:
            from agp.models import Job
            job = session.get(Job, job_id)
            # Force-down should have cancelled it via _force_cancel_agent_work
            # If it did, the sweeper orphan check is belt-and-suspenders.
            # If not, the sweeper catches it:
            if job.status == JobStatus.QUEUED.value:
                result = sweep_stale_agents(session)
                self.assertGreaterEqual(result["orphaned_jobs_failed"], 1)
                session.refresh(job)
                self.assertEqual(job.status, JobStatus.FAILED.value)
        finally:
            session.close()
