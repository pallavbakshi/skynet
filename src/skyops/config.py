"""Load and merge skyops.toml + skyops.local.toml configuration."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into *base* (override wins)."""
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


@dataclass
class StackConfig:
    mode: str = "docker"
    compose_file: str = "compose.phase3.yaml"
    project_name: str = "agp"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 7860


@dataclass
class DatabaseConfig:
    url: str = "postgresql+psycopg://agp:agp@localhost:5432/agp"


@dataclass
class RedisConfig:
    url: str = "redis://localhost:6379/0"


@dataclass
class S3Config:
    endpoint_url: str = "http://localhost:9000"
    access_key_id: str = "minioadmin"
    secret_access_key: str = "minioadmin"
    bucket: str = "agp-artifacts"


@dataclass
class SecurityConfig:
    operator_token: str = ""
    runtime_token: str = ""


@dataclass
class RuntimeConfig:
    host_kind: str = "tmux"
    adapter_kind: str = "codex"


@dataclass
class MonitoringConfig:
    prometheus: bool = True
    grafana: bool = True


@dataclass
class SkyopsConfig:
    """Full skyops configuration tree."""

    stack: StackConfig = field(default_factory=StackConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    s3: S3Config = field(default_factory=S3Config)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    agents: dict[str, dict[str, str]] = field(default_factory=dict)
    capabilities: dict[str, dict[str, str]] = field(default_factory=dict)

    # Where the config was loaded from (set after load)
    _config_path: Path | None = field(default=None, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SkyopsConfig:
        """Build a SkyopsConfig from a raw TOML dict."""
        return cls(
            stack=_build(StackConfig, data.get("stack", {})),
            server=_build(ServerConfig, data.get("server", {})),
            database=_build(DatabaseConfig, data.get("database", {})),
            redis=_build(RedisConfig, data.get("redis", {})),
            s3=_build(S3Config, data.get("s3", {})),
            security=_build(SecurityConfig, data.get("security", {})),
            runtime=_build(RuntimeConfig, data.get("runtime", {})),
            monitoring=_build(MonitoringConfig, data.get("monitoring", {})),
            agents=data.get("agents", {}),
            capabilities=data.get("capabilities", {}),
        )

    def to_display_dict(self, *, mask_secrets: bool = True) -> dict[str, Any]:
        """Return a nested dict suitable for display, with secrets masked."""
        import dataclasses

        out: dict[str, Any] = {}
        for f in dataclasses.fields(self):
            if f.name.startswith("_"):
                continue
            val = getattr(self, f.name)
            if dataclasses.is_dataclass(val):
                out[f.name] = dataclasses.asdict(val)
            else:
                out[f.name] = val

        if mask_secrets:
            _mask(out)
        return out


_SECRET_KEYS = {"operator_token", "runtime_token", "secret_access_key", "access_key_id"}


def _mask(d: dict[str, Any]) -> None:
    for key, val in d.items():
        if isinstance(val, dict):
            _mask(val)
        elif key in _SECRET_KEYS and isinstance(val, str) and val:
            d[key] = val[:2] + "***" + val[-2:] if len(val) > 4 else "****"


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Build a dataclass from a dict, ignoring unknown keys."""
    import dataclasses

    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _load_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from *start* looking for ``skyops.toml``."""
    cur = (start or Path.cwd()).resolve()
    for d in [cur, *cur.parents]:
        candidate = d / "skyops.toml"
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None) -> SkyopsConfig:
    """Load and merge ``skyops.toml`` + ``skyops.local.toml``.

    If *path* is None, searches upward from cwd.
    Raises ``FileNotFoundError`` when no ``skyops.toml`` exists.
    """
    if path is None:
        path = find_config()
    if path is None:
        raise FileNotFoundError(
            "skyops.toml not found. Run `skyops init` to create one."
        )

    base = _load_toml(path)
    local_path = path.parent / "skyops.local.toml"
    if local_path.is_file():
        local = _load_toml(local_path)
        base = _deep_merge(base, local)

    cfg = SkyopsConfig.from_dict(base)
    cfg._config_path = path
    return cfg
