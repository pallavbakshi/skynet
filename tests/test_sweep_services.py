"""Focused service-layer tests for sweep operations.

These test the sweep functions at the service seam, independent of
the full control-plane route layer.
"""

from __future__ import annotations

from datetime import timedelta

from agp.db import SessionLocal
from agp.enums import AgentStatus, HealthStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus
from agp.models import Agent, Job, Lease, Message, Run, Runtime, utc_now
from agp.services.sweep import (
    sweep_draining_agents,
    sweep_draining_runtimes,
    sweep_expired_leases,
    sweep_idle_agents,
    sweep_stale_runtimes,
)

from _base import AgpTestCase


def _seed_full_claim(session, *, agent_id="agt_sw", runtime_id="rtm_sw", job_id="job_sw", lease_ttl=30):
    """Create agent, runtime, message, job, run, and active lease."""
    now = utc_now()
    rt = Runtime(runtime_id=runtime_id, hostname="test", status=RuntimeStatus.BUSY.value, health_status=HealthStatus.HEALTHY.value, last_seen_at=now, last_heartbeat_at=now, created_at=now, updated_at=now)
    ag = Agent(agent_id=agent_id, capability_id="cap_python", queue_id=f"agent:{agent_id}", status=AgentStatus.BUSY.value, last_seen_at=now, created_at=now, updated_at=now)
    msg = Message(message_id=f"msg_{job_id}", target_type="agent", target_id=agent_id, text="sweep test", created_at=now)
    job = Job(job_id=job_id, message_id=msg.message_id, target_agent_id=agent_id, target_queue=f"agent:{agent_id}", status=JobStatus.RUNNING.value, created_at=now, updated_at=now)
    run = Run(run_id=f"run_{job_id}", job_id=job_id, agent_id=agent_id, runtime_id=runtime_id, attempt=1, status=RunStatus.RUNNING.value, started_at=now, created_at=now)
    lease = Lease(lease_id=f"lease_{job_id}", run_id=run.run_id, agent_id=agent_id, runtime_id=runtime_id, fencing_token=1, status=LeaseStatus.ACTIVE.value, expires_at=now + timedelta(seconds=lease_ttl), created_at=now)
    for obj in (rt, ag, msg, job, run, lease):
        session.add(obj)
    session.flush()
    return {"runtime": rt, "agent": ag, "job": job, "run": run, "lease": lease}


class SweepExpiredLeasesTest(AgpTestCase):
    def test_expired_lease_requeues_job(self) -> None:
        session = SessionLocal()
        try:
            entities = _seed_full_claim(session, lease_ttl=-1)
            session.commit()
            result = sweep_expired_leases(session)
            self.assertEqual(result["expired_leases"], 1)
            self.assertEqual(result["requeued_jobs"], 1)
            session.refresh(entities["job"])
            self.assertEqual(entities["job"].status, JobStatus.QUEUED.value)
        finally:
            session.close()

    def test_expired_lease_fails_job_at_max_retries(self) -> None:
        session = SessionLocal()
        try:
            entities = _seed_full_claim(session, lease_ttl=-1)
            entities["job"].retry_count = 2
            entities["job"].max_retries = 3
            session.commit()
            result = sweep_expired_leases(session)
            self.assertEqual(result["failed_jobs"], 1)
            session.refresh(entities["job"])
            self.assertEqual(entities["job"].status, JobStatus.FAILED.value)
        finally:
            session.close()


class SweepIdleAgentsTest(AgpTestCase):
    def test_idle_agent_terminated_after_timeout(self) -> None:
        session = SessionLocal()
        try:
            now = utc_now()
            rt = Runtime(runtime_id="rtm_idle", hostname="h", status="idle", health_status="healthy", last_seen_at=now, last_heartbeat_at=now, created_at=now, updated_at=now)
            ag = Agent(agent_id="agt_idle", capability_id="cap_python", queue_id="agent:agt_idle", status=AgentStatus.IDLE.value, last_seen_at=now - timedelta(seconds=999), created_at=now, updated_at=now)
            session.add(rt)
            session.add(ag)
            session.commit()
            result = sweep_idle_agents(session, idle_timeout_seconds=10)
            self.assertEqual(result["terminated_agents"], 1)
        finally:
            session.close()


class SweepStaleRuntimesTest(AgpTestCase):
    def test_stale_runtime_goes_offline(self) -> None:
        session = SessionLocal()
        try:
            now = utc_now()
            rt = Runtime(runtime_id="rtm_stale", hostname="h", status=RuntimeStatus.IDLE.value, health_status=HealthStatus.HEALTHY.value, last_seen_at=now, last_heartbeat_at=now - timedelta(seconds=999), created_at=now, updated_at=now)
            session.add(rt)
            session.commit()
            result = sweep_stale_runtimes(session, stale_timeout_seconds=10, degraded_timeout_seconds=5)
            self.assertEqual(result["offline_runtimes"], 1)
        finally:
            session.close()


class SweepDrainingTest(AgpTestCase):
    def test_draining_agent_terminates_when_clear(self) -> None:
        session = SessionLocal()
        try:
            now = utc_now()
            rt = Runtime(runtime_id="rtm_dr", hostname="h", status="idle", health_status="healthy", last_seen_at=now, last_heartbeat_at=now, created_at=now, updated_at=now)
            ag = Agent(agent_id="agt_dr", capability_id="cap_python", queue_id="agent:agt_dr", status=AgentStatus.DRAINING.value, last_seen_at=now, created_at=now, updated_at=now)
            session.add(rt)
            session.add(ag)
            session.commit()
            result = sweep_draining_agents(session)
            self.assertEqual(result["terminated_agents"], 1)
        finally:
            session.close()

    def test_draining_runtime_returns_to_idle(self) -> None:
        session = SessionLocal()
        try:
            now = utc_now()
            rt = Runtime(runtime_id="rtm_drt", hostname="h", status=RuntimeStatus.DRAINING.value, health_status=HealthStatus.DRAINING.value, last_seen_at=now, last_heartbeat_at=now, created_at=now, updated_at=now)
            session.add(rt)
            session.commit()
            result = sweep_draining_runtimes(session)
            self.assertEqual(result["resumed_runtimes"], 1)
            session.refresh(rt)
            self.assertEqual(rt.status, RuntimeStatus.IDLE.value)
        finally:
            session.close()
