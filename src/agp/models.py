"""ORM models for the AGP scaffold.

CheckConstraints mirror the constraints in migrations/0001_initial.sql
so that ORM-level validation is consistent with the Postgres schema.
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column

from agp.db import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Capability(Base):
    __tablename__ = "capabilities"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_capabilities_name_version"),
        CheckConstraint("resource_tier IN ('small', 'medium', 'large', 'gpu')", name="chk_capabilities_resource_tier"),
        CheckConstraint("queue_mode IN ('agent')", name="chk_capabilities_queue_mode"),
    )

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
        CheckConstraint(
            "status IN ('registering', 'idle', 'busy', 'degraded', 'offline', 'draining')",
            name="chk_runtimes_status",
        ),
        CheckConstraint(
            "health_status IN ('healthy', 'degraded', 'unreachable', 'draining')",
            name="chk_runtimes_health_status",
        ),
    )

    runtime_id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id", ondelete="SET NULL"), nullable=True, unique=True)
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
        Index("ix_agents_status_heartbeat", "status", "last_heartbeat_at"),
        CheckConstraint(
            "status IN ('idle', 'busy', 'draining')",
            name="chk_agents_status",
        ),
    )

    agent_id: Mapped[str] = mapped_column(String, primary_key=True)
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    queue_id: Mapped[str] = mapped_column(String, unique=True)
    status: Mapped[str] = mapped_column(String)
    workspace_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    @hybrid_property
    def registered_at(self) -> datetime:
        """PRD alias for created_at."""
        return self.created_at


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        CheckConstraint("target_type IN ('agent', 'capability')", name="chk_messages_target_type"),
    )

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_type: Mapped[str] = mapped_column(String)
    target_id: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reply_to_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_agent_status_created", "target_agent_id", "status", "created_at"),
        CheckConstraint(
            "status IN ('accepted', 'queued', 'running', 'interrupt_requested', 'completed', 'failed', 'cancelled', 'blocked')",
            name="chk_jobs_status",
        ),
        CheckConstraint("retry_count >= 0", name="chk_jobs_retry_count"),
        CheckConstraint("max_retries >= 0", name="chk_jobs_max_retries"),
        CheckConstraint("target_queue <> ''", name="chk_jobs_target_presence"),
    )

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.message_id"))
    target_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.agent_id", ondelete="SET NULL"), nullable=True)
    target_queue: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    latest_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    result_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    output_contract_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QueueDeliveryRecord(Base):
    __tablename__ = "queue_deliveries"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'delivered', 'acked', 'dead_lettered')",
            name="chk_deliveries_state",
        ),
        CheckConstraint("delivery_attempt >= 0", name="chk_deliveries_attempt"),
    )

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
        CheckConstraint(
            "status IN ('created', 'leased', 'running', 'recovering', 'completed', 'failed', 'abandoned', 'cancelled')",
            name="chk_runs_status",
        ),
        CheckConstraint("attempt > 0", name="chk_runs_attempt"),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"))
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
        CheckConstraint("fencing_token > 0", name="chk_leases_fencing_token"),
        CheckConstraint("status IN ('active', 'expired', 'released')", name="chk_leases_status"),
    )

    lease_id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
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
        CheckConstraint("size_bytes >= 0", name="chk_artifacts_size"),
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
    __table_args__ = ()

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.job_id"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("artifacts.artifact_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)


class RunArtifact(Base):
    __tablename__ = "run_artifacts"
    __table_args__ = (
        CheckConstraint(
            "role IN ('prompt', 'transcript_log', 'exec_log', 'result', 'failure_evidence', 'extraction_diagnostics')",
            name="chk_run_artifacts_role",
        ),
    )

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
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    runtime_id: Mapped[str | None] = mapped_column(ForeignKey("runtimes.runtime_id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String)
    body_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EventJobLink(Base):
    __tablename__ = "event_job_links"
    __table_args__ = (
        CheckConstraint(
            "relation IN ('primary', 'source', 'child', 'related')",
            name="chk_event_links_relation",
        ),
    )

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


class Nudge(Base):
    __tablename__ = "nudges"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'delivered', 'expired')", name="chk_nudges_status"),
        CheckConstraint("source IN ('human', 'job_completion', 'agenda_setter', 'system')", name="chk_nudges_source"),
    )

    nudge_id: Mapped[str] = mapped_column(String, primary_key=True)
    target_agent_id: Mapped[str] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.job_id"), nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SystemMetadata(Base):
    __tablename__ = "system_metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
