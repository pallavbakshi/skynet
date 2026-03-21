"""Pydantic schemas for the AGP control plane MVP."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from agp.db import current_release_version
from agp.enums import AgentStatus, HealthStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus


class HealthResponse(BaseModel):
    status: str = "ok"
    components: dict[str, str] = Field(default_factory=dict)


class SendTarget(BaseModel):
    type: str
    id: str


class SendMessagePayload(BaseModel):
    text: str
    metadata: dict = Field(default_factory=dict)


class SendMessageRequest(BaseModel):
    target: SendTarget
    message: SendMessagePayload
    detach_policy: dict = Field(default_factory=dict)


class ListParams(BaseModel):
    offset: int = 0
    limit: int = 50


class AgentUpRequest(BaseModel):
    agent_id: str | None = None
    capability_id: str
    workspace_ref: str | None = None
    assigned_runtime_id: str | None = None


class AgentDownRequest(BaseModel):
    mode: Literal["drain", "terminate", "force"] = "drain"


class RuntimeRegisterRequest(BaseModel):
    runtime_id: str | None = None
    hostname: str
    release_version: str = Field(default_factory=current_release_version)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RotateOperatorTokensRequest(BaseModel):
    operator_bearer_token: str | None = None
    operator_token_roles_json: dict[str, str] = Field(default_factory=dict)


class RotateRuntimeTokensRequest(BaseModel):
    runtime_bearer_token: str | None = None
    runtime_active_tokens_json: list[str] = Field(default_factory=list)


class ClaimRunRequest(BaseModel):
    runtime_id: str
    agent_id: str | None = None
    capability_id: str | None = None
    lease_ttl_seconds: int = 30


class HeartbeatRequest(BaseModel):
    runtime_id: str
    lease_id: str
    fencing_token: int
    extend_seconds: int = 30


class ProgressRequest(BaseModel):
    runtime_id: str
    lease_id: str
    fencing_token: int
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RecoveryRequest(BaseModel):
    runtime_id: str
    lease_id: str
    fencing_token: int
    details: dict[str, Any] = Field(default_factory=dict)


class ArtifactReference(BaseModel):
    role: str
    storage_ref: str
    content_type: str = "text/plain"
    checksum: str = ""
    size_bytes: int = 0


class CompleteRunRequest(BaseModel):
    runtime_id: str
    lease_id: str
    fencing_token: int
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class FailRunRequest(BaseModel):
    runtime_id: str
    lease_id: str
    fencing_token: int
    error: str
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class CancelRunRequest(BaseModel):
    runtime_id: str
    lease_id: str
    fencing_token: int
    reason: str = "interrupt_requested"


class HandoffTarget(BaseModel):
    type: Literal["agent", "capability"]
    id: str


class HandoffRequest(BaseModel):
    targets: list[HandoffTarget]
    message: SendMessagePayload
    artifact_ids: list[str] = Field(default_factory=list)


class ArtifactUploadRequest(BaseModel):
    namespace: str
    job_id: str
    name: str
    content: str
    role: str
    content_type: str = "text/plain"


class CapabilitySeedRequest(BaseModel):
    capability_id: str
    name: str
    version: str = "v1"
    image_ref: str = ""
    model_ref: str = ""
    resource_tier: str = "small"
    permission_profile: str = "default"
    queue_mode: Literal["agent", "capability_pool"] = "agent"
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)


class PageEnvelope(BaseModel):
    offset: int
    limit: int
    has_more: bool


class EventResponse(BaseModel):
    event_id: str
    event_seq: int
    event_type: str
    created_at: str
    body: dict[str, Any]


class AgentResponse(BaseModel):
    agent_id: str
    capability_id: str
    assigned_runtime_id: str | None
    queue_id: str
    status: AgentStatus
    workspace_ref: str | None


class RuntimeResponse(BaseModel):
    runtime_id: str
    hostname: str
    status: RuntimeStatus
    health_status: HealthStatus
    metadata: dict[str, Any]


class JobResponse(BaseModel):
    job_id: str
    message_id: str
    target_agent_id: str | None
    target_queue: str
    status: JobStatus
    retry_count: int
    max_retries: int
    latest_run_id: str | None
    result_artifact_id: str | None


class RunResponse(BaseModel):
    run_id: str
    job_id: str
    agent_id: str
    runtime_id: str
    attempt: int
    status: RunStatus


class LeaseResponse(BaseModel):
    lease_id: str
    run_id: str
    agent_id: str
    runtime_id: str
    fencing_token: int
    status: LeaseStatus
    expires_at: str
