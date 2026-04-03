"""Focused backend-contract tests for queue implementations.

These tests verify the formal queue backend contract independent of
the full control-plane route layer.
"""

from __future__ import annotations

import threading

from sqlalchemy import select

from agp.db import SessionLocal
from agp.enums import JobStatus
from agp.models import Agent, Job, Message, QueueDeliveryRecord, utc_now
from agp.queue_backend import (
    DbQueueBackend,
    DeliveryTableQueueBackend,
    InMemoryBrokerQueueBackend,
    RedisQueueBackend,
    QueueDelivery,
    queue_backlog_info,
    queue_backlogs_by_target_queue,
    queue_oldest_queued_at,
)
import agp.queue_backend as queue_backend_module
from agp.services.jobs import _block_job, _unblock_job

from tests._base import AgpTestCase, FakeRedisClient


def _seed_agent(session, agent_id: str = "agt_q") -> Agent:
    agent = Agent(
        agent_id=agent_id,
        capabilities=["python"],
        metadata_json={},
        queue_id=f"agent:{agent_id}",
        status="idle",
        last_heartbeat_at=utc_now(),
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


def _seed_job_with_status(
    session,
    *,
    agent_id: str = "agt_q",
    job_id: str,
    status: str,
) -> Job:
    job = _seed_job(session, agent_id=agent_id, job_id=job_id)
    job.status = status
    session.flush()
    return job


def _concurrent_dequeue_attempts(backend, *, target_queue: str) -> list[QueueDelivery | None]:
    barrier = threading.Barrier(2)
    results: list[QueueDelivery | None] = [None, None]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        session = SessionLocal()
        try:
            barrier.wait()
            results[index] = backend.dequeue_candidate(session, target_queues=[target_queue])
            session.commit()
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return results


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

    def test_dequeue_is_atomic_under_concurrent_claims(self) -> None:
        backend = DbQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job_id = job.job_id
            session.commit()
        finally:
            session.close()

        results = _concurrent_dequeue_attempts(backend, target_queue="agent:agt_q")
        claimed = [delivery for delivery in results if delivery is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].job_id, job_id)

        verify = SessionLocal()
        try:
            refreshed = verify.get(Job, job_id)
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertEqual(refreshed.status, JobStatus.ACCEPTED.value)
        finally:
            verify.close()

    def test_peek_queue_counts_only_queued_jobs(self) -> None:
        backend = DbQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            _seed_job_with_status(session, job_id="job_q1", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_q2", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_a1", status=JobStatus.ACCEPTED.value)
            session.commit()
            self.assertEqual(backend.peek_queue(session, target_queues=["agent:agt_q"]), 2)
        finally:
            session.close()

    def test_queue_oldest_queued_at_returns_oldest_matching_timestamp(self) -> None:
        session = SessionLocal()
        try:
            _seed_agent(session)
            older = _seed_job_with_status(session, job_id="job_old", status=JobStatus.QUEUED.value)
            newer = _seed_job_with_status(session, job_id="job_new", status=JobStatus.QUEUED.value)
            older.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=older.updated_at.tzinfo)
            newer.updated_at = utc_now().__class__(2001, 1, 1, tzinfo=newer.updated_at.tzinfo)
            session.commit()
            oldest = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(oldest)
            assert oldest is not None
            self.assertEqual(oldest.isoformat(), "2000-01-01T00:00:00+00:00")
        finally:
            session.close()

    def test_queue_oldest_queued_at_returns_none_for_empty_or_nonmatching_queues(self) -> None:
        session = SessionLocal()
        try:
            _seed_agent(session)
            _seed_job(session)
            session.commit()
            self.assertIsNone(queue_oldest_queued_at(session, target_queues=[]))
            self.assertIsNone(queue_oldest_queued_at(session, target_queues=["agent:missing"]))
        finally:
            session.close()

    def test_queue_backlog_info_returns_zero_for_empty_or_nonmatching_queues(self) -> None:
        session = SessionLocal()
        try:
            _seed_agent(session)
            _seed_job(session)
            session.commit()
            self.assertEqual(queue_backlog_info(session, target_queues=[]), {"queue_depth": 0, "oldest_queued_at": None})
            self.assertEqual(
                queue_backlog_info(session, target_queues=["agent:missing"]),
                {"queue_depth": 0, "oldest_queued_at": None},
            )
            self.assertEqual(queue_backlogs_by_target_queue(session, target_queues=[]), {})
            self.assertEqual(
                queue_backlogs_by_target_queue(session, target_queues=["agent:missing"]),
                {"agent:missing": {"queue_depth": 0, "oldest_queued_at": None}},
            )
        finally:
            session.close()

    def test_queue_oldest_queued_at_refreshes_when_work_requeues(self) -> None:
        backend = DbQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            backend.release_unclaimed(session, delivery=delivery)
            session.commit()
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
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

    def test_dequeue_is_atomic_under_concurrent_claims(self) -> None:
        backend = DeliveryTableQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job_id = job.job_id
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
        finally:
            session.close()

        results = _concurrent_dequeue_attempts(backend, target_queue="agent:agt_q")
        claimed = [delivery for delivery in results if delivery is not None]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].job_id, job_id)

        verify = SessionLocal()
        try:
            record = verify.scalars(
                select(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job_id)
            ).first()
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.state, "delivered")
            self.assertEqual(record.delivery_attempt, 1)
        finally:
            verify.close()

    def test_peek_queue_counts_only_queued_jobs(self) -> None:
        backend = DeliveryTableQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            _seed_job_with_status(session, job_id="job_q1", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_q2", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_a1", status=JobStatus.ACCEPTED.value)
            session.commit()
            self.assertEqual(backend.peek_queue(session, target_queues=["agent:agt_q"]), 2)
        finally:
            session.close()

    def test_queue_oldest_queued_at_refreshes_when_work_requeues(self) -> None:
        backend = DeliveryTableQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            backend.release_unclaimed(session, delivery=delivery)
            session.commit()
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
        finally:
            session.close()

    def test_queue_oldest_queued_at_refreshes_when_stale_delivery_redrives(self) -> None:
        backend = DeliveryTableQueueBackend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            from agp.models import QueueDeliveryRecord
            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            record.last_delivered_at = utc_now().__class__(2000, 1, 1, tzinfo=record.last_delivered_at.tzinfo)
            session.commit()
            result = backend.redrive_stale_deliveries(session, visibility_timeout_seconds=0, max_delivery_attempts=3)
            session.commit()
            self.assertEqual(result["redriven_deliveries"], 1)
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
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

    def test_cancelled_job_is_not_redelivered(self) -> None:
        backend = InMemoryBrokerQueueBackend()
        backend.reset()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            job.status = JobStatus.CANCELLED.value
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNone(delivery)
        finally:
            session.close()

    def test_peek_queue_counts_only_queued_jobs(self) -> None:
        backend = InMemoryBrokerQueueBackend()
        backend.reset()
        session = SessionLocal()
        try:
            _seed_agent(session)
            _seed_job_with_status(session, job_id="job_q1", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_q2", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_a1", status=JobStatus.ACCEPTED.value)
            session.commit()
            self.assertEqual(backend.peek_queue(session, target_queues=["agent:agt_q"]), 2)
        finally:
            session.close()

    def test_queue_oldest_queued_at_refreshes_when_work_requeues(self) -> None:
        backend = InMemoryBrokerQueueBackend()
        backend.reset()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            backend.enqueue_job(session, job=job)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            backend.release_unclaimed(session, delivery=delivery)
            session.commit()
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
        finally:
            session.close()

    def test_queue_oldest_queued_at_refreshes_when_stale_delivery_redrives(self) -> None:
        backend = InMemoryBrokerQueueBackend()
        backend.reset()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            backend.enqueue_job(session, job=job)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            result = backend.redrive_stale_deliveries(session, visibility_timeout_seconds=0, max_delivery_attempts=3)
            session.commit()
            self.assertEqual(result["redriven_deliveries"], 1)
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
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

    def test_phase2_recovery_sql_delivered_missing_redis_inflight(self) -> None:
        """Crash after SQL marked 'delivered' but before Redis inflight written."""
        from datetime import datetime, timezone
        from agp.models import QueueDeliveryRecord
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            # Normal dequeue — marks SQL as "delivered" and writes Redis inflight
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            session.commit()
            # Simulate crash: remove the Redis inflight entry but leave SQL as "delivered"
            backend.client.hdel(backend._inflight_hash_key(), delivery.delivery_id)
            backend.client.srem(backend._inflight_jobs_key(), job.job_id)
            # Backdate the SQL record so it's past the visibility timeout
            rec = session.get(QueueDeliveryRecord, delivery.delivery_id)
            rec.last_delivered_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            session.commit()
            # Phase 2 should find the orphaned SQL "delivered" record
            result = backend.redrive_stale_deliveries(
                session, visibility_timeout_seconds=0, max_delivery_attempts=3
            )
            session.commit()
            self.assertEqual(result["redriven_deliveries"], 1)
            # Verify the delivery is back in SQL as "pending"
            session.refresh(rec)
            self.assertEqual(rec.state, "pending")
            # Verify it's back in the Redis queue and can be dequeued
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(d2)
            self.assertEqual(d2.job_id, job.job_id)
        finally:
            session.close()

    def test_peek_queue_counts_only_queued_jobs(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            _seed_job_with_status(session, job_id="job_q1", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_q2", status=JobStatus.QUEUED.value)
            _seed_job_with_status(session, job_id="job_a1", status=JobStatus.ACCEPTED.value)
            session.commit()
            self.assertEqual(backend.peek_queue(session, target_queues=["agent:agt_q"]), 2)
        finally:
            session.close()

    def test_queue_oldest_queued_at_refreshes_when_work_requeues(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            backend.release_unclaimed(session, delivery=delivery)
            session.commit()
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
        finally:
            session.close()

    def test_queue_oldest_queued_at_refreshes_when_stale_delivery_redrives(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(delivery)
            from agp.models import QueueDeliveryRecord
            record = session.get(QueueDeliveryRecord, delivery.delivery_id)
            assert record is not None
            record.last_delivered_at = utc_now().__class__(2000, 1, 1, tzinfo=record.last_delivered_at.tzinfo)
            session.commit()
            result = backend.redrive_stale_deliveries(
                session, visibility_timeout_seconds=0, max_delivery_attempts=3
            )
            session.commit()
            self.assertEqual(result["redriven_deliveries"], 1)
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
        finally:
            session.close()

    def test_phase3_recovery_sql_pending_missing_redis_pending(self) -> None:
        """Crash after Redis LPOP but before SQL update to 'delivered'."""
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            # Manually simulate the crash window:
            # Remove job from Redis list and pending set (as if LPOP succeeded)
            # but SQL still says "pending" (process died before SQL transition)
            backend.client.lpop(backend._queue_key("agent:agt_q"))
            backend.client.srem(backend._pending_set_key("agent:agt_q"), job.job_id)
            # Confirm Redis is empty — dequeue returns None
            d = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNone(d)
            # Phase 3 should detect SQL pending with missing Redis entry
            result = backend.redrive_stale_deliveries(
                session, visibility_timeout_seconds=0, max_delivery_attempts=3
            )
            session.commit()
            self.assertGreaterEqual(result["redriven_deliveries"], 1)
            # Now dequeue should work again
            d2 = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(d2)
            self.assertEqual(d2.job_id, job.job_id)
        finally:
            session.close()

    def test_phase3_recovery_refreshes_queue_oldest_age(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            job.updated_at = utc_now().__class__(2000, 1, 1, tzinfo=job.updated_at.tzinfo)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            backend.client.lpop(backend._queue_key("agent:agt_q"))
            backend.client.srem(backend._pending_set_key("agent:agt_q"), job.job_id)
            result = backend.redrive_stale_deliveries(
                session, visibility_timeout_seconds=0, max_delivery_attempts=3
            )
            session.commit()
            self.assertGreaterEqual(result["redriven_deliveries"], 1)
            refreshed = queue_oldest_queued_at(session, target_queues=["agent:agt_q"])
            self.assertIsNotNone(refreshed)
            assert refreshed is not None
            self.assertGreaterEqual((utc_now() - refreshed).total_seconds(), 0)
            self.assertLess((utc_now() - refreshed).total_seconds(), 30)
        finally:
            session.close()

    def test_phase3_recovery_does_not_resurrect_cancelled_job(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()

            backend.client.lpop(backend._queue_key("agent:agt_q"))
            backend.client.srem(backend._pending_set_key("agent:agt_q"), job.job_id)
            job.status = JobStatus.CANCELLED.value
            session.commit()

            result = backend.redrive_stale_deliveries(
                session, visibility_timeout_seconds=0, max_delivery_attempts=3
            )
            session.commit()

            self.assertEqual(result["redriven_deliveries"], 0)
            self.assertIsNone(backend.dequeue_candidate(session, target_queues=["agent:agt_q"]))

            record = session.scalars(
                select(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job.job_id)
            ).one()
            self.assertEqual(record.state, "acked")
            self.assertIsNotNone(record.acked_at)
        finally:
            session.close()

    def test_phase3_recovery_does_not_dead_letter_terminal_job(self) -> None:
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()

            backend.client.lpop(backend._queue_key("agent:agt_q"))
            backend.client.srem(backend._pending_set_key("agent:agt_q"), job.job_id)
            job.status = JobStatus.FAILED.value
            job.retry_count = job.max_retries
            session.commit()

            result = backend.redrive_stale_deliveries(
                session, visibility_timeout_seconds=0, max_delivery_attempts=3
            )
            session.commit()

            self.assertEqual(result["redriven_deliveries"], 0)
            self.assertEqual(result["dead_lettered_deliveries"], 0)

            record = session.scalars(
                select(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job.job_id)
            ).one()
            self.assertEqual(record.state, "acked")
            self.assertIsNotNone(record.acked_at)
            self.assertIsNone(record.dead_lettered_at)
        finally:
            session.close()

    def test_phase2_dead_letters_after_max_attempts(self) -> None:
        """Phase 2 recovery dead-letters if delivery_attempt >= max."""
        from datetime import datetime, timezone
        from agp.models import QueueDeliveryRecord
        backend = self._make_backend()
        session = SessionLocal()
        try:
            _seed_agent(session)
            job = _seed_job(session)
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()
            # Exhaust delivery attempts through normal redrive cycles
            for _ in range(3):
                delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
                if delivery is None:
                    break
                rec = session.get(QueueDeliveryRecord, delivery.delivery_id)
                rec.last_delivered_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
                session.commit()
                backend.redrive_stale_deliveries(
                    session, visibility_timeout_seconds=0, max_delivery_attempts=3
                )
                session.commit()
            # Now create the orphan scenario for phase 2:
            # Dequeue one more time (attempt 4, should be at limit)
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_q"])
            if delivery is not None:
                # Orphan the Redis inflight entry
                backend.client.hdel(backend._inflight_hash_key(), delivery.delivery_id)
                backend.client.srem(backend._inflight_jobs_key(), job.job_id)
                rec = session.get(QueueDeliveryRecord, delivery.delivery_id)
                rec.last_delivered_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
                session.commit()
                result = backend.redrive_stale_deliveries(
                    session, visibility_timeout_seconds=0, max_delivery_attempts=3
                )
                session.commit()
                self.assertGreaterEqual(result["dead_lettered_deliveries"], 1)
                session.refresh(rec)
                self.assertEqual(rec.state, "dead_lettered")
            else:
                # Job was already dead-lettered during earlier cycles
                from sqlalchemy import select as sa_select
                rec = session.scalars(
                    sa_select(QueueDeliveryRecord).where(QueueDeliveryRecord.job_id == job.job_id)
                ).first()
                self.assertEqual(rec.state, "dead_lettered")
        finally:
            session.close()

    def test_block_and_unblock_keep_redis_transport_consistent(self) -> None:
        session = SessionLocal()
        try:
            from agp.config import settings
            from agp.queue_backend import get_queue_backend

            settings.queue_backend = "redis"
            settings.redis_url = "redis://fake"
            settings.redis_queue_key_prefix = "test"
            backend = get_queue_backend("redis")
            _seed_agent(session, agent_id="agt_block")
            job = _seed_job(session, agent_id="agt_block", job_id="job_block")
            session.commit()
            backend.enqueue_job(session, job=job)
            session.commit()

            _block_job(session, job=job, reason="operator-block")
            session.commit()
            self.assertIsNone(backend.dequeue_candidate(session, target_queues=["agent:agt_block"]))

            _unblock_job(session, job=job, reason="operator-unblock")
            session.commit()
            delivery = backend.dequeue_candidate(session, target_queues=["agent:agt_block"])
            self.assertIsNotNone(delivery)
            assert delivery is not None
            self.assertEqual(delivery.job_id, job.job_id)
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
