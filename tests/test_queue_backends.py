"""Focused backend-contract tests for queue implementations.

These tests verify the formal queue backend contract independent of
the full control-plane route layer.
"""

from __future__ import annotations

from agp.db import SessionLocal
from agp.enums import JobStatus
from agp.models import Agent, Job, Message, utc_now
from agp.queue_backend import (
    DbQueueBackend,
    DeliveryTableQueueBackend,
    InMemoryBrokerQueueBackend,
    RedisQueueBackend,
    QueueDelivery,
)
import agp.queue_backend as queue_backend_module

from _base import AgpTestCase, FakeRedisClient


def _seed_agent(session, agent_id: str = "agt_q") -> Agent:
    agent = Agent(
        agent_id=agent_id,
        capability_id="cap_python",
        queue_id=f"agent:{agent_id}",
        status="idle",
        last_seen_at=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(agent)
    session.flush()
    return agent


def _seed_job(session, agent_id: str = "agt_q", job_id: str = "job_q") -> Job:
    msg = Message(
        message_id=f"msg_{job_id}",
        target_type="agent",
        target_id=agent_id,
        text="queue test",
        created_at=utc_now(),
    )
    session.add(msg)
    session.flush()
    job = Job(
        job_id=job_id,
        message_id=msg.message_id,
        target_agent_id=agent_id,
        target_queue=f"agent:{agent_id}",
        status=JobStatus.QUEUED.value,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(job)
    session.flush()
    return job


class DbBackendContractTest(AgpTestCase):
    """DbQueueBackend: polls jobs table directly, no delivery records."""

    def test_dequeue_returns_queued_job(self) -> None:
        backend = DbQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery.job_id, job.job_id)
        finally:
            session.close()

    def test_dequeue_returns_none_when_no_queued_jobs(self) -> None:
        backend = DbQueueBackend()
        session = SessionLocal()
        try:
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNone(delivery)
        finally:
            session.close()

    def test_enqueue_and_ack_are_noops(self) -> None:
        backend = DbQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            delivery = QueueDelivery(delivery_id="d1", job_id=job.job_id, target_queue="agent:agt_q", delivery_attempt=1)
            backend.ack_claim(session, delivery=delivery, job=job)
            backend.release_unclaimed(session, delivery=delivery)
        finally:
            session.close()


class DeliveryTableBackendContractTest(AgpTestCase):
    """DeliveryTableQueueBackend: SQL-only delivery persistence."""

    def test_enqueue_dequeue_ack_lifecycle(self) -> None:
        backend = DeliveryTableQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery.job_id, job.job_id)
            self.assertEqual(delivery.delivery_attempt, 1)
            backend.ack_claim(session, delivery=delivery, job=job)
            session.commit()
            # After ack, no more deliveries
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNone(d2)
        finally:
            session.close()

    def test_release_makes_delivery_available_again(self) -> None:
        backend = DeliveryTableQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            backend.release_unclaimed(session, delivery=delivery)
            session.commit()
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(d2)
            self.assertEqual(d2.job_id, job.job_id)
        finally:
            session.close()

    def test_redrive_dead_letters_after_max_attempts(self) -> None:
        backend = DeliveryTableQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            # Deliver 3 times without acking
            for _ in range(3):
                d = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
                self.assertIsNotNone(d)
                # Simulate stale by backdating
                from agp.models import QueueDeliveryRecord
                rec = session.get(QueueDeliveryRecord, d.delivery_id)
                rec.last_delivered_at = utc_now().__class__(2000, 1, 1, tzinfo=rec.last_delivered_at.tzinfo)
                session.commit()
                result = backend.redrive_stale_deliveries(session, visibility_timeout_seconds=0, max_delivery_attempts=3)
                session.commit()
            from agp.models import QueueDeliveryRecord as QDR
            from sqlalchemy import select
            dead = session.scalars(select(QDR).where(QDR.job_id == job.job_id, QDR.state == "dead_lettered")).first()
            self.assertIsNotNone(dead)
        finally:
            session.close()


class InMemoryBrokerContractTest(AgpTestCase):
    """InMemoryBrokerQueueBackend: process-local, no SQL delivery records."""

    def test_enqueue_dequeue_ack(self) -> None:
        backend = InMemoryBrokerQueueBackend()
        backend.reset()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery.job_id, job.job_id)
            backend.ack_claim(session, delivery=delivery, job=job)
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNone(d2)
        finally:
            session.close()

    def test_release_returns_to_front_of_queue(self) -> None:
        backend = InMemoryBrokerQueueBackend()
        backend.reset()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            backend.release_unclaimed(session, delivery=delivery)
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(d2)
            self.assertEqual(d2.job_id, job.job_id)
        finally:
            session.close()

    def test_redrive_dead_letters_after_max_attempts(self) -> None:
        backend = InMemoryBrokerQueueBackend()
        backend.reset()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            # Cycle dequeue → redrive to exhaust attempts
            for i in range(3):
                d = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
                self.assertIsNotNone(d, f"dequeue {i+1} should return delivery")
                result = backend.redrive_stale_deliveries(session, visibility_timeout_seconds=0, max_delivery_attempts=3)
                if i < 2:
                    self.assertEqual(result["redriven_deliveries"], 1)
                else:
                    self.assertEqual(result["dead_lettered_deliveries"], 1)
            d = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNone(d)
        finally:
            session.close()


class RedisBackendContractTest(AgpTestCase):
    """RedisQueueBackend: Redis transport with SQL shadow records."""

    def setUp(self) -> None:
        super().setUp()
        queue_backend_module._REDIS_CLIENT_FACTORY = lambda url: FakeRedisClient()

    def _make_backend(self) -> RedisQueueBackend:
        backend = RedisQueueBackend(redis_url="redis://fake", key_prefix="test")
        return backend

    def test_enqueue_dequeue_ack_lifecycle(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery.job_id, job.job_id)
            self.assertEqual(delivery.delivery_attempt, 1)
            backend.ack_claim(session, delivery=delivery, job=job)
            session.commit()
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNone(d2)
        finally:
            session.close()

    def test_release_makes_delivery_available_again(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            backend.release_unclaimed(session, delivery=delivery)
            session.commit()
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(d2)
            self.assertEqual(d2.job_id, job.job_id)
        finally:
            session.close()

    def test_sql_shadow_record_created_on_enqueue(self) -> None:
        """Verify SQL is the durable authority — a shadow record is created."""
        from sqlalchemy import select as sa_select
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            from agp.models import QueueDeliveryRecord
            rec = session.scalars(sa_select(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job.job_id)).first()
            self.assertIsNotNone(rec)
            self.assertEqual(rec.state, "pending")
        finally:
            session.close()


class ArtifactStoreContractTest(AgpTestCase):
    """LocalFsArtifactStore: write/read/exists lifecycle."""

    def test_write_read_round_trip(self) -> None:
        from agp.artifact_store import get_artifact_store
        store = get_artifact_store("localfs", self._artifact_root())
        stored = store.write_text(
            namespace="test-ns",
            job_id="job_art_test",
            name="result.txt",
            content="hello world",
            role="result",
        )
        self.assertTrue(store.exists(storage_ref=stored.storage_ref))
        content = store.read_text(storage_ref=stored.storage_ref)
        self.assertEqual(content, "hello world")
        self.assertEqual(stored.role, "result")
        self.assertGreater(stored.size_bytes, 0)
        self.assertNotEqual(stored.checksum, "")

    def test_exists_returns_false_for_missing(self) -> None:
        from agp.artifact_store import get_artifact_store
        store = get_artifact_store("localfs", self._artifact_root())
        self.assertFalse(store.exists(storage_ref="nonexistent/path.txt"))

    def test_read_returns_none_for_missing(self) -> None:
        from agp.artifact_store import get_artifact_store
        store = get_artifact_store("localfs", self._artifact_root())
        content = store.read_text(storage_ref="nonexistent/path.txt")
        self.assertIsNone(content)

    def _artifact_root(self):
        from agp.config import settings
        return settings.artifact_root
