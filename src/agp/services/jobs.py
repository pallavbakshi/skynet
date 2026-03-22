"""Job domain operations — send, handoff, block/unblock, exhaustion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agp.enums import AgentStatus, ArtifactKind, JobStatus, LeaseStatus, RunStatus, RuntimeStatus
from agp.models import (
    Artifact,
    Handoff,
    HandoffArtifact,
    HandoffJob,
    Job,
    JobArtifact,
    Lease,
    Message,
    Run,
    utc_now,
)
from agp.services._helpers import (
    _capability_queue_for,
    _ensure_inline_runtime,
    _new_id,
    _queue_backend,
    _queue_for_target,
    _record_agent_binding,
    _require_agent,
    _require_capability,
    _require_job,
    _write_control_plane_artifact,
)
from agp.services.events import _create_event


def _block_job(db: Session, *, job: Job, reason: str) -> None:
    if job.status != JobStatus.QUEUED.value:
        raise HTTPException(status_code=409, detail=f"job cannot be blocked from state {job.status}")
    job.status = JobStatus.BLOCKED.value
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type="job.blocked", body={"reason": reason})


def _unblock_job(db: Session, *, job: Job, reason: str) -> None:
    if job.status != JobStatus.BLOCKED.value:
        raise HTTPException(status_code=409, detail=f"job cannot be unblocked from state {job.status}")
    job.status = JobStatus.QUEUED.value
    job.updated_at = utc_now()
    _create_event(db, job_id=job.job_id, event_type="job.queued", body={"target_queue": job.target_queue, "reason": reason})


def _fail_exhausted_queued_jobs(db: Session, *, target_queues: list[str]) -> int:
    jobs = db.scalars(
        select(Job).where(
            Job.status == JobStatus.QUEUED.value,
            Job.retry_count >= Job.max_retries,
            Job.target_queue.in_(target_queues),
        )
    ).all()
    for job in jobs:
        job.status = JobStatus.FAILED.value
        job.updated_at = utc_now()
        _create_event(db, job_id=job.job_id, event_type="job.failed", body={"reason": "retry_budget_exhausted", "retry_count": job.retry_count})
    return len(jobs)


def _handoff_ancestor_job_ids(db: Session, job_id: str, max_depth: int = 50) -> set[str]:
    ancestors: set[str] = set()
    current = job_id
    for _ in range(max_depth):
        link = db.scalar(select(HandoffJob.handoff_id).where(HandoffJob.job_id == current))
        if link is None:
            break
        parent = db.scalar(select(Handoff.source_job_id).where(Handoff.handoff_id == link))
        if parent is None or parent in ancestors:
            break
        ancestors.add(parent)
        current = parent
    return ancestors


# ── Protocol orchestration: send_message, inline execution, handoff ──


@dataclass
class SendResult:
    """Result of a send_message operation."""
    kind: str  # "inline_result" or "accepted_async"
    job_id: str
    status: str
    message_id: str
    result_artifact_id: str | None = None
    target: dict | None = None


def create_and_enqueue_job(
    db: Session,
    *,
    target_type: str,
    target_id: str,
    text: str,
    metadata: dict,
) -> tuple[Message, Job]:
    """Create a message and queued job — the core dispatch path."""
    if target_type == "agent":
        agent = _require_agent(db, target_id)
        if agent.status == AgentStatus.TERMINATED.value:
            raise HTTPException(status_code=409, detail=f"agent is terminated: {target_id}")
        if agent.status == AgentStatus.DRAINING.value:
            raise HTTPException(status_code=409, detail=f"agent is draining: {target_id}")
    elif target_type == "capability":
        _require_capability(db, target_id)
    else:
        raise HTTPException(status_code=400, detail="target.type must be agent or capability")

    message = Message(
        message_id=_new_id("msg"),
        target_type=target_type,
        target_id=target_id,
        text=text,
        metadata_json=metadata,
    )
    db.add(message)
    db.flush()

    job = Job(
        job_id=_new_id("job"),
        message_id=message.message_id,
        target_agent_id=target_id if target_type == "agent" else None,
        target_queue=(
            _queue_for_target(target_type, target_id)
            if target_type == "agent"
            else _capability_queue_for(db, target_id)
        ),
        status=JobStatus.QUEUED.value,
        max_retries=3,
    )
    db.add(job)
    db.flush()

    _create_event(db, job_id=job.job_id, event_type="job.accepted", body={"message_id": message.message_id, "target_type": target_type, "target_id": target_id})
    _create_event(db, job_id=job.job_id, event_type="job.queued", body={"target_queue": job.target_queue})
    _queue_backend().enqueue_job(db, job=job)

    return message, job


def execute_inline(db: Session, *, job: Job, agent, message: Message) -> SendResult:
    """Execute a job inline on the control-plane runtime.

    Creates run, lease, artifacts, and completes the full lifecycle
    synchronously.  Returns a SendResult with kind="inline_result".
    """
    from agp.services.runs import _store_terminal_artifacts

    runtime = _ensure_inline_runtime(db)
    attempt = 1
    run = Run(
        run_id=_new_id("run"),
        job_id=job.job_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        attempt=attempt,
        status=RunStatus.RUNNING.value,
        started_at=utc_now(),
    )
    db.add(run)
    db.flush()
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="run.created", body={"attempt": attempt})
    lease = Lease(
        lease_id=_new_id("lease"),
        run_id=run.run_id,
        agent_id=agent.agent_id,
        runtime_id=runtime.runtime_id,
        fencing_token=attempt,
        status=LeaseStatus.ACTIVE.value,
        expires_at=utc_now() + timedelta(seconds=30),
    )
    db.add(lease)
    job.status = JobStatus.RUNNING.value
    job.latest_run_id = run.run_id
    job.updated_at = utc_now()
    agent.status = AgentStatus.BUSY.value
    _record_agent_binding(db, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, status="active")
    agent.assigned_runtime_id = runtime.runtime_id
    runtime.status = RuntimeStatus.BUSY.value
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="lease.acquired", body={"lease_id": lease.lease_id, "fencing_token": lease.fencing_token, "expires_at": lease.expires_at.isoformat()})
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="run.running", body={"started_by": runtime.runtime_id})
    artifacts = [
        _write_control_plane_artifact(job_id=job.job_id, name="prompt.txt", content=message.text),
        _write_control_plane_artifact(job_id=job.job_id, name="transcript.txt", content=f"inline\nmessage={message.text}\n"),
        _write_control_plane_artifact(job_id=job.job_id, name="exec.txt", content="inline-exec\n"),
        _write_control_plane_artifact(job_id=job.job_id, name="result.txt", content=f"inline result for {message.text}\n"),
    ]
    result_artifact_id, _ = _store_terminal_artifacts(db, job_id=job.job_id, run_id=run.run_id, artifacts=artifacts)
    run.status = RunStatus.COMPLETED.value
    run.finished_at = utc_now()
    lease.status = LeaseStatus.RELEASED.value
    lease.released_at = utc_now()
    job.status = JobStatus.COMPLETED.value
    job.result_artifact_id = result_artifact_id
    job.updated_at = utc_now()
    agent.status = AgentStatus.IDLE.value
    runtime.status = RuntimeStatus.IDLE.value
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="lease.released", body={"lease_id": lease.lease_id})
    _create_event(db, job_id=job.job_id, run_id=run.run_id, agent_id=agent.agent_id, runtime_id=runtime.runtime_id, event_type="run.completed", body={"artifact_ids": [a.artifact_id for a in db.scalars(select(Artifact).where(Artifact.run_id == run.run_id)).all()]})
    _create_event(db, job_id=job.job_id, event_type="job.completed", body={"status": job.status})
    return SendResult(kind="inline_result", job_id=job.job_id, status=JobStatus.COMPLETED.value, message_id=message.message_id, result_artifact_id=result_artifact_id)


def execute_handoff(
    db: Session,
    *,
    source_job: Job,
    targets: list,
    message_payload: Any,
    artifact_ids: list[str],
) -> dict:
    """Execute a handoff — create child jobs, validate/propagate artifacts."""
    ancestor_ids = _handoff_ancestor_job_ids(db, source_job.job_id)
    if ancestor_ids:
        for target in targets:
            if target.type == "agent":
                agent_ancestor_jobs = db.scalars(
                    select(Job.job_id).where(
                        Job.target_agent_id == target.id,
                        Job.status.in_([JobStatus.RUNNING.value, JobStatus.QUEUED.value]),
                        Job.job_id.in_(ancestor_ids),
                    )
                ).all()
                if agent_ancestor_jobs:
                    raise HTTPException(status_code=409, detail=f"handoff cycle detected: agent {target.id} has ancestor job {agent_ancestor_jobs[0]} in its chain")

    handoff = Handoff(handoff_id=_new_id("hnd"), source_job_id=source_job.job_id)
    db.add(handoff)
    db.flush()
    child_job_ids: list[str] = []
    for target in targets:
        if target.type == "agent":
            _require_agent(db, target.id)
        else:
            _require_capability(db, target.id)
        msg = Message(
            message_id=_new_id("msg"),
            target_type=target.type,
            target_id=target.id,
            text=message_payload.text,
            metadata_json=message_payload.metadata,
        )
        db.add(msg)
        db.flush()
        child = Job(
            job_id=_new_id("job"),
            message_id=msg.message_id,
            target_agent_id=target.id if target.type == "agent" else None,
            target_queue=_queue_for_target(target.type, target.id) if target.type == "agent" else _capability_queue_for(db, target.id),
            status=JobStatus.QUEUED.value,
            max_retries=3,
        )
        db.add(child)
        db.flush()
        db.add(HandoffJob(handoff_id=handoff.handoff_id, job_id=child.job_id))
        child_job_ids.append(child.job_id)
        _queue_backend().enqueue_job(db, job=child)

    validated_artifact_ids: list[str] = []
    for aid in artifact_ids:
        artifact = db.get(Artifact, aid)
        if artifact is None:
            raise HTTPException(status_code=400, detail=f"handoff artifact not found: {aid}")
        if artifact.job_id != source_job.job_id:
            raise HTTPException(status_code=400, detail=f"artifact {aid} does not belong to source job {source_job.job_id}")
        db.add(HandoffArtifact(handoff_id=handoff.handoff_id, artifact_id=aid))
        validated_artifact_ids.append(aid)
    for child_job_id in child_job_ids:
        for aid in validated_artifact_ids:
            artifact = db.get(Artifact, aid)
            if artifact is not None:
                db.add(JobArtifact(job_id=child_job_id, artifact_id=aid, role=artifact.kind))

    _create_event(
        db, job_id=source_job.job_id, event_type="handoff.created",
        body={"handoff_id": handoff.handoff_id, "source_job_id": source_job.job_id, "source_artifact_ids": artifact_ids, "created_job_ids": child_job_ids},
        related_jobs=[(source_job.job_id, "source"), *[(cid, "child") for cid in child_job_ids]],
    )
    db.commit()
    return {"handoff_id": handoff.handoff_id, "source_job_id": source_job.job_id, "child_job_ids": child_job_ids}
