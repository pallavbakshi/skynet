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
class WorkspaceProfileConfig:
    mode: str = "shared_fs"
    workspace_ref: str = ""
    mounts: list[str] = field(default_factory=list)
    repo_url: str = ""
    repo_ref: str = "master"
    repo_name: str = ""


@dataclass
class HostProfileConfig:
    mount_sources: dict[str, str] = field(default_factory=dict)
    git_root: str = ""
    worktree_root: str = ""


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
    workspace_profiles: dict[str, WorkspaceProfileConfig] = field(default_factory=dict)
    host_profiles: dict[str, HostProfileConfig] = field(default_factory=dict)

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
            workspace_profiles={
                name: _build(WorkspaceProfileConfig, raw)
                for name, raw in data.get("workspace_profiles", {}).items()
            },
            host_profiles={
                name: _build(
                    HostProfileConfig,
                    {
                        "mount_sources": raw.get("mount_sources", {}),
                        "git_root": raw.get("git_root", ""),
                        "worktree_root": raw.get("worktree_root", ""),
                    },
                )
                for name, raw in data.get("host_profiles", {}).items()
            },
        )

    def resolve_workspace_profile(self, name: str) -> WorkspaceProfileConfig:
        profile = self.workspace_profiles.get(name)
        if profile is None:
            raise KeyError(f"workspace profile not found: {name}")
        return profile

    def resolve_host_profile(self, name: str) -> HostProfileConfig:
        profile = self.host_profiles.get(name)
        if profile is None:
            raise KeyError(f"host profile not found: {name}")
        return profile

    def default_host_profile_name(self) -> str | None:
        if "default" in self.host_profiles:
            return "default"
        if len(self.host_profiles) == 1:
            return next(iter(self.host_profiles))
        return None

    def resolve_agent_workspace(
        self, agent_id: str, *, host_profile: str | None = None
    ) -> dict[str, Any]:
        if agent_id not in self.agents:
            raise KeyError(f"agent not found: {agent_id}")
        agent = dict(self.agents.get(agent_id, {}))
        profile_name = agent.get("workspace_profile")
        profile = self.resolve_workspace_profile(profile_name) if profile_name else WorkspaceProfileConfig()
        mode = agent.get("workspace_mode") or profile.mode or "shared_fs"
        requires_host_profile = mode in {"git", "worktree"} or any(
            isinstance(mount, str) and mount.startswith("@")
            for mount in [*profile.mounts, *(agent.get("mounts", []) or [])]
        )
        resolved_host_name = host_profile or self.default_host_profile_name()
        if requires_host_profile and resolved_host_name is None and len(self.host_profiles) > 1:
            raise ValueError("host_profile is required when multiple host_profiles are configured")
        resolved_host = (
            self.resolve_host_profile(resolved_host_name)
            if resolved_host_name
            else HostProfileConfig()
        )
        workspace_ref = agent.get("workspace_ref") or profile.workspace_ref or None
        mounts: list[str] = []
        prepare_commands: list[str] = []
        managed_mount_targets: list[str] = []

        if mode == "shared_fs":
            mounts.extend(self._resolve_mounts(profile.mounts, resolved_host))
            self._merge_supplemental_mounts(
                mounts,
                self._resolve_mounts(agent.get("mounts", []) or [], resolved_host),
            )
        elif mode == "git":
            if not workspace_ref:
                raise ValueError(f"agent {agent_id} git workspace requires workspace_ref")
            if not resolved_host.git_root:
                raise ValueError(f"agent {agent_id} git workspace requires host_profile.git_root")
            repo_url = agent.get("repo_url") or profile.repo_url
            repo_ref = agent.get("repo_ref") or profile.repo_ref or "master"
            if not repo_url:
                raise ValueError(f"agent {agent_id} git workspace requires repo_url")
            repo_name = agent.get("repo_name") or profile.repo_name or agent_id
            host_path = f"{resolved_host.git_root.rstrip('/')}/{repo_name}"
            mounts.append(f"{host_path}:{workspace_ref}")
            managed_mount_targets.append(workspace_ref)
            self._merge_supplemental_mounts(
                mounts,
                self._resolve_mounts(profile.mounts, resolved_host),
                exclude_target=workspace_ref,
            )
            self._merge_supplemental_mounts(
                mounts,
                self._resolve_mounts(agent.get("mounts", []) or [], resolved_host),
                exclude_target=workspace_ref,
            )
            prepare_commands.extend(
                [
                    f'mkdir -p "{resolved_host.git_root}"',
                    f'if [ ! -d "{host_path}/.git" ]; then git clone "{repo_url}" "{host_path}"; fi',
                    f'git -C "{host_path}" fetch --all --prune',
                    (
                        f'git -C "{host_path}" checkout "{repo_ref}"'
                        f' || git -C "{host_path}" checkout -b "{repo_ref}" "origin/{repo_ref}"'
                    ),
                ]
            )
        elif mode == "worktree":
            if not workspace_ref:
                raise ValueError(f"agent {agent_id} worktree workspace requires workspace_ref")
            if not resolved_host.git_root or not resolved_host.worktree_root:
                raise ValueError(
                    f"agent {agent_id} worktree workspace requires host_profile.git_root and worktree_root"
                )
            repo_url = agent.get("repo_url") or profile.repo_url
            repo_ref = agent.get("repo_ref") or profile.repo_ref or "master"
            if not repo_url:
                raise ValueError(f"agent {agent_id} worktree workspace requires repo_url")
            repo_name = agent.get("repo_name") or profile.repo_name or profile_name or "repo"
            worktree_name = agent.get("worktree_name") or agent_id
            repo_path = f"{resolved_host.git_root.rstrip('/')}/{repo_name}"
            worktree_path = f"{resolved_host.worktree_root.rstrip('/')}/{worktree_name}"
            mounts.append(f"{worktree_path}:{workspace_ref}")
            self._merge_supplemental_mounts(mounts, [f"{resolved_host.git_root}:{resolved_host.git_root}"])
            self._merge_supplemental_mounts(mounts, [f"{resolved_host.worktree_root}:{resolved_host.worktree_root}"])
            managed_mount_targets.extend([workspace_ref, resolved_host.git_root, resolved_host.worktree_root])
            self._merge_supplemental_mounts(
                mounts,
                self._resolve_mounts(profile.mounts, resolved_host),
                exclude_target=workspace_ref,
            )
            self._merge_supplemental_mounts(
                mounts,
                self._resolve_mounts(agent.get("mounts", []) or [], resolved_host),
                exclude_target=workspace_ref,
            )
            prepare_commands.extend(
                [
                    f'mkdir -p "{resolved_host.git_root}" "{resolved_host.worktree_root}"',
                    f'if [ ! -d "{repo_path}/.git" ]; then git clone "{repo_url}" "{repo_path}"; fi',
                    f'git -C "{repo_path}" fetch --all --prune',
                    (
                        f'if [ ! -e "{worktree_path}/.git" ]; then '
                        f'git -C "{repo_path}" worktree add "{worktree_path}" "{repo_ref}"; '
                        f'else git -C "{worktree_path}" fetch --all --prune; '
                        f'git -C "{worktree_path}" checkout "{repo_ref}"'
                        f' || git -C "{worktree_path}" checkout -b "{repo_ref}" "origin/{repo_ref}"; fi'
                    ),
                ]
            )
        else:
            raise ValueError(f"unsupported workspace mode: {mode}")
        return {
            "agent_id": agent_id,
            "workspace_profile": profile_name,
            "workspace_mode": mode,
            "host_profile": resolved_host_name,
            "workspace_ref": workspace_ref,
            "mounts": mounts,
            "managed_mount_targets": managed_mount_targets,
            "prepare_commands": prepare_commands,
        }

    def resolve_agent_workspace_ref(self, agent_id: str) -> str | None:
        if agent_id not in self.agents:
            raise KeyError(f"agent not found: {agent_id}")
        agent = dict(self.agents.get(agent_id, {}))
        profile_name = agent.get("workspace_profile")
        profile = self.resolve_workspace_profile(profile_name) if profile_name else WorkspaceProfileConfig()
        return agent.get("workspace_ref") or profile.workspace_ref or None

    def _resolve_mounts(
        self, mounts: list[str], host_profile: HostProfileConfig
    ) -> list[str]:
        resolved: list[str] = []
        for mount in mounts:
            if not isinstance(mount, str):
                continue
            if mount.startswith("@"):
                source_name, sep, target = mount[1:].partition(":")
                if not sep or not target:
                    raise ValueError(f"invalid symbolic mount: {mount}")
                source_path = host_profile.mount_sources.get(source_name)
                if not source_path:
                    raise ValueError(f"mount source not found for {mount}")
                resolved.append(f"{source_path}:{target}")
            else:
                resolved.append(mount)
        return resolved

    def _merge_supplemental_mounts(
        self,
        current: list[str],
        incoming: list[str],
        *,
        exclude_target: str | None = None,
    ) -> list[str]:
        for mount in incoming:
            if exclude_target and self._mount_target(mount) == exclude_target:
                continue
            if mount not in current:
                current.append(mount)
        return current

    def _mount_target(self, mount: str) -> str:
        parts = mount.split(":")
        if len(parts) < 2:
            return ""
        return parts[1]

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


def build_agp_env(cfg: SkyopsConfig) -> dict[str, str]:
    """Build a base environment dict with DB URL and security tokens from *cfg*.

    Callers that need extra keys (Redis, S3, etc.) should update the returned dict.
    """
    import os

    env = os.environ.copy()
    env["AGP_DATABASE_URL"] = cfg.database.url
    if cfg.security.operator_token:
        env["AGP_OPERATOR_BEARER_TOKEN"] = cfg.security.operator_token
        env["AGP_OPERATOR_TOKEN"] = cfg.security.operator_token
    if cfg.security.runtime_token:
        env["AGP_RUNTIME_BEARER_TOKEN"] = cfg.security.runtime_token
    return env


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
