"""Core enums derived from the AGP specs."""

from enum import StrEnum


class JobStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPT_REQUESTED = "interrupt_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class RunStatus(StrEnum):
    CREATED = "created"
    LEASED = "leased"
    RUNNING = "running"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"
    CANCELLED = "cancelled"


class AgentStatus(StrEnum):
    PROVISIONING = "provisioning"
    IDLE = "idle"
    BUSY = "busy"
    DEGRADED = "degraded"
    DRAINING = "draining"
    TERMINATED = "terminated"


class RuntimeStatus(StrEnum):
    REGISTERING = "registering"
    IDLE = "idle"
    BUSY = "busy"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    DRAINING = "draining"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    DRAINING = "draining"


class LeaseStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"


class ArtifactKind(StrEnum):
    PROMPT = "prompt"
    TRANSCRIPT_LOG = "transcript_log"
    EXEC_LOG = "exec_log"
    RESULT = "result"
    FAILURE_EVIDENCE = "failure_evidence"
