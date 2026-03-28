"""Pydantic schemas for the AGP control plane MVP."""

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from agp.db import current_release_version
from agp.enums import AgentStatus, HealthStatus, JobStatus, LeaseStatus, RunStatus, RuntimeStatus

T = TypeVar("T")


class OkResponse(BaseModel, Generic[T]):
    """Standard API envelope for successful responses."""
    model_config = ConfigDict(extra="allow")
    ok: bool = True
    data: T


class PageInfo(BaseModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class PagedData(BaseModel, Generic[T]):
    """Paginated list data payload."""
    model_config = ConfigDict(extra="allow")
    items: list[T]
    page: PageInfo


class HealthResponse(BaseModel):
    status: str = "ok"
    components: dict[str, str] = Field(default_factory=dict)


class SendTarget(BaseModel):
    type: str
    id: str


class OutputContract(BaseModel):
    format: str = "json"
    json_schema: dict[str, Any] = Field(default_factory=dict)


class SendMessagePayload(BaseModel):
    text: str
    metadata: dict = Field(default_factory=dict)
    output_contract: OutputContract | None = None
    conversation_id: str | None = None
    reply_to_message_id: str | None = None


class SendMessageRequest(BaseModel):
    target: SendTarget
    message: SendMessagePayload
    detach_policy: dict = Field(default_factory=dict)


class ListParams(BaseModel):
    offset: int = 0
    limit: int = 50


class AgentUpRequest(BaseModel):
    agent_id: str | None = None
    capabilities: list[str] | None = None
    metadata: dict[str, Any] | None = None
    workspace_ref: str | None = None


class AgentPatchRequest(BaseModel):
    workspace_ref: str | None = None


class CreateNudgeRequest(BaseModel):
    target_agent_id: str
    priority: int = 2  # 1=human, 2=job_completion, 3=agenda_setter, 4=system
    source: str = "human"
    payload: str
    job_id: str | None = None


class AgentDownRequest(BaseModel):
    mode: Literal["drain", "force"] = "drain"


class AgentInterruptRequest(BaseModel):
    purge: bool = False


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
    capability: str | None = None
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
    queue_mode: Literal["agent"] = "agent"
    runtime_requirements: dict[str, Any] = Field(default_factory=dict)


class PageEnvelope(BaseModel):
    offset: int
    limit: int
    has_more: bool


class EventResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    event_id: str
    event_seq: int
    event_type: str
    created_at: Any
    body: dict[str, Any]


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    agent_id: str
    capabilities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    queue_id: str
    status: str
    workspace_ref: str | None = None
    last_heartbeat_at: Any = None


class RuntimeResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    runtime_id: str
    hostname: str
    status: str
    health_status: str
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    job_id: str
    message_id: str
    target_agent_id: str | None = None
    target_queue: str
    status: str
    retry_count: int
    max_retries: int
    latest_run_id: str | None = None
    result_artifact_id: str | None = None
    output_contract_json: dict[str, Any] | None = None
    conversation_id: str | None = None


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    run_id: str
    job_id: str
    agent_id: str | None = None
    runtime_id: str
    attempt: int
    status: str


class LeaseResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    lease_id: str
    run_id: str
    agent_id: str | None = None
    runtime_id: str
    fencing_token: int
    status: str
    expires_at: Any
