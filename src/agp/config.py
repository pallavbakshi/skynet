"""Configuration for the AGP scaffold."""

from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    model_config = SettingsConfigDict(env_prefix="AGP_", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 7860
    database_url: str = "sqlite+pysqlite:///./agp.db"
    app_name: str = "AGP Control Plane"
    artifact_root: Path = Path(".agp-artifacts")
    log_root: Path = Path(".agp-logs")
    observability_log_rotation_bytes: int = 1_000_000
    observability_control_plane_log_retention_days: int = 30
    observability_runtime_log_retention_days: int = 30
    artifact_backend: str = "localfs"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str = "minioadmin"
    s3_secret_access_key: str = "minioadmin"
    s3_bucket: str = "agp-artifacts"
    s3_region: str = "us-east-1"
    s3_force_path_style: bool = True
    queue_backend: str = "delivery_table"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_queue_key_prefix: str = "agp"
    queue_visibility_timeout_seconds: int = 30
    queue_max_delivery_attempts: int = 3
    lease_heartbeat_interval_seconds: int = 10
    agent_heartbeat_grace_seconds: int = 60
    agent_idle_timeout_seconds: int = 300  # legacy, replaced by agent_heartbeat_grace_seconds
    runtime_stale_timeout_seconds: int = 90
    runtime_degraded_timeout_seconds: int = 45
    observability_unreachable_runtime_threshold: int = 1
    observability_expired_lease_alert_threshold: int = 3
    observability_dead_letter_alert_threshold: int = 1
    observability_terminal_failure_sample_size: int = 3
    observability_terminal_failure_rate_threshold: float = 0.5
    observability_queue_depth_alert_threshold: int = 5
    observability_stale_queue_age_seconds: int = 900
    observability_alert_webhook_url: str | None = None
    observability_alert_webhook_timeout_seconds: float = 5.0
    operator_bearer_token: str | None = None
    operator_token_roles_json: dict[str, str] = {}
    runtime_active_tokens_json: list[str] = []
    runtime_bearer_token: str | None = None
    runtime_terminal_host_kind: str = "inprocess"
    runtime_agent_adapter_kind: str = "default"
    wezterm_workspace: str = "agp"
    wezterm_domain: str = ""
    codex_begin_marker: str = "AGP_RUN_BEGIN"
    codex_result_marker: str = "AGP_RUN_RESULT"
    codex_poll_interval_seconds: float = 0.25
    codex_max_polls: int = 20
    codex_bootstrap_settle_seconds: float = 0.0
    codex_idle_timeout_polls: int = 0
    codex_health_check_interval_polls: int = 10
    codex_cli_command: str = "codex"
    codex_tui_mode: bool = False
    codex_idle_poll_seconds: float = 2.0
    codex_idle_after: int = 3
    codex_idle_timeout_seconds: float = 0.0
    codex_session_mode: str = "ephemeral"
    claude_code_cli_command: str = "claude"
    claude_code_idle_poll_seconds: float = 2.0
    claude_code_idle_after: int = 3
    claude_code_idle_timeout_seconds: float = 0.0
    claude_code_session_mode: str = "ephemeral"
    claude_code_bootstrap_settle_seconds: float = 0.0
    wezterm_default_cwd: str = ""
    scrollback_lines: int = 5000
    wezterm_scrollback_lines: int | None = None
    tmux_scrollback_lines: int = 5000
    tmux_default_cwd: str = ""
    tmux_session_prefix: str = "agp"
    output_checkpoint_dir: Path = Path(".agp-checkpoints")

    @model_validator(mode="before")
    @classmethod
    def _apply_legacy_wezterm_scrollback_lines(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "scrollback_lines" in data:
            return data
        legacy_value = data.get("wezterm_scrollback_lines")
        if legacy_value is None:
            return data
        return {**data, "scrollback_lines": legacy_value}

    @model_validator(mode="after")
    def _validate_backend_requirements(self) -> "Settings":
        if self.artifact_backend == "s3":
            missing: list[str] = []
            if not self.s3_endpoint_url:
                missing.append("AGP_S3_ENDPOINT_URL")
            if not self.s3_bucket:
                missing.append("AGP_S3_BUCKET")
            if not self.s3_access_key_id:
                missing.append("AGP_S3_ACCESS_KEY_ID")
            if not self.s3_secret_access_key:
                missing.append("AGP_S3_SECRET_ACCESS_KEY")
            if missing:
                raise ValueError(
                    "artifact backend 's3' requires: " + ", ".join(missing)
                )
        if self.queue_visibility_timeout_seconds <= self.lease_heartbeat_interval_seconds:
            import warnings
            warnings.warn(
                f"queue_visibility_timeout_seconds ({self.queue_visibility_timeout_seconds}) should be "
                f"greater than lease_heartbeat_interval_seconds ({self.lease_heartbeat_interval_seconds}) "
                f"to prevent premature broker redelivery",
                stacklevel=2,
            )
        return self


settings = Settings()
