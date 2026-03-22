"""ORM models for the AGP scaffold."""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agp.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Capability(Base):
    __tablename__ = "capabilities"

    capability_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    image_ref: Mapped[str] = mapped_column(String)
    model_ref: Mapped[str] = mapped_column(String)
    resource_tier: Mapped[str] = mapped_column(String)
    permission_profile: Mapped[str] = mapped_column(String)
    queue_mode: Mapped[str] = mapped_column(String)
    runtime_requirements_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @property
    def runtime_requirements(self) -> dict:
        """Spec-compatible alias for runtime_requirements_json."""
        return self.runtime_requirements_json


class Runtime(Base):
    __tablename__ = "runtimes"
    __table_args__ = (
        Index("ix_runtimes_status_lastseen", "status", "last_seen_at"),
    )

    runtime_id: Mapped[str] = mapped_column(String, primary_key=True)
    hostname: Mapped[str] = mapped_column(String)
    release_version: Mapped[str] = mapped_column(String, default="0.1.0")
    status: Mapped[str] = mapped_column(String)
    health_status: Mapped[str] = mapped_column(String)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index("ix_agents_status_created", "status", "created_at"),
    )

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.capability_id"))
    assigned_runtime_id: Mapped[str | None] = mapped_column(ForeignKey("runtimes.runtime_id"), nullable=True)
    queue_id: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String)
    workspace_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AgentRuntimeBinding(Base):
    __tablename__ = "agent_runtime_bindings"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id"), primary_key=True)
    runtime_id: Mapped[str] = mapped_column(ForeignKey("runtimes.runtime_id"), primary_key=True)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, default=utc_now)
    binding_status: Mapped[str] = mapped_column(String)


class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_type: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_agent_status_created", "target_agent_id", "status", "created_at"),
    )

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.message_id"))
    target_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id"), nullable=True)
    target_queue: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    latest_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    result_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QueueDeliveryRecord(Base):
    __tablename__ = "queue_deliveries"

    delivery_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"))
    target_queue: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    delivery_attempt: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt", name="uq_runs_job_attempt"),
        Index("ix_runs_job_attempt", "job_id", "attempt"),
        Index("ix_runs_runtime_status_created", "runtime_id", "status", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id"))
    runtime_id: Mapped[str] = mapped_column(ForeignKey("runtimes.runtime_id"))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (
        Index("ix_leases_run_status", "run_id", "status"),
        Index("ix_leases_runtime_status_expires", "runtime_id", "status", "expires_at"),
    )

    lease_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agent_id"))
    runtime_id: Mapped[str] = mapped_column(ForeignKey("runtimes.runtime_id"))
    fencing_token: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_job_created", "job_id", "created_at"),
        Index("ix_artifacts_run_created", "run_id", "created_at"),
    )

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id"), nullable=True)
    kind: Mapped[str] = mapped_column(String)
    content_type: Mapped[str] = mapped_column(String)
    storage_ref: Mapped[str] = mapped_column(String)
    checksum: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JobArtifact(Base):
    __tablename__ = "job_artifacts"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.artifact_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)


class RunArtifact(Base):
    __tablename__ = "run_artifacts"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.artifact_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)


class Handoff(Base):
    __tablename__ = "handoffs"

    handoff_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class HandoffArtifact(Base):
    __tablename__ = "handoff_artifacts"

    handoff_id: Mapped[str] = mapped_column(ForeignKey("handoffs.handoff_id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.artifact_id"), primary_key=True)


class HandoffJob(Base):
    __tablename__ = "handoff_jobs"

    handoff_id: Mapped[str] = mapped_column(ForeignKey("handoffs.handoff_id"), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_job_seq", "job_id", "event_seq"),
        Index("ix_events_run_seq", "run_id", "event_seq"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_seq: Mapped[int] = mapped_column(Integer, unique=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id"), nullable=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id"), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id"), nullable=True)
    runtime_id: Mapped[str | None] = mapped_column(ForeignKey("runtimes.runtime_id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String)
    body_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EventJobLink(Base):
    __tablename__ = "event_job_links"

    event_id: Mapped[str] = mapped_column(ForeignKey("events.event_id"), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)
    relation: Mapped[str] = mapped_column(String, primary_key=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index("ix_idempotency_expires", "expires_at"),
    )

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String, primary_key=True)
    request_hash: Mapped[str] = mapped_column(String)
    response_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HealthRecord(Base):
    __tablename__ = "health_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String)
    health_status: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(String)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CapabilityPool(Base):
    __tablename__ = "capability_pools"

    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.capability_id"), primary_key=True)
    queue_id: Mapped[str] = mapped_column(String, unique=True)
    routing_policy: Mapped[str] = mapped_column(String, default="least_recent")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Nudge(Base):
    __tablename__ = "nudges"

    nudge_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_agent_id: Mapped[str] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer)  # 1=highest, 4=lowest
    source: Mapped[str] = mapped_column(String)  # human, job_completion, agenda_setter, system
    payload: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, delivered, expired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemMetadata(Base):
    __tablename__ = "system_metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
