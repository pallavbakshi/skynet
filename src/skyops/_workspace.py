"""``skyops workspace`` — resolve and validate agent workspace plans."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer

from skyops.config import load_config

workspace_app = typer.Typer(help="Workspace resolution and validation commands.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _mount_source(mount: str) -> str:
    parts = mount.split(":")
    return parts[0] if parts else ""


def _mount_target(mount: str) -> str:
    parts = mount.split(":")
    return parts[1] if len(parts) > 1 else ""


@workspace_app.command("resolve")
def workspace_resolve(
    agent_id: str = typer.Argument(help="Agent ID to resolve workspace for."),
    host_profile: str | None = typer.Option(None, "--host-profile", help="Host profile name."),
) -> None:
    """Resolve the effective workspace plan for an agent."""
    cfg = load_config()
    try:
        result = cfg.resolve_agent_workspace(agent_id, host_profile=host_profile)
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    _emit(result)


@workspace_app.command("validate")
def workspace_validate(
    agent_id: str = typer.Argument(help="Agent ID to validate workspace for."),
    host_profile: str | None = typer.Option(None, "--host-profile", help="Host profile name."),
) -> None:
    """Validate the effective workspace plan and host prerequisites."""
    cfg = load_config()
    try:
        resolved = cfg.resolve_agent_workspace(agent_id, host_profile=host_profile)
    except (KeyError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    mode = resolved["workspace_mode"]
    managed_targets = set(resolved.get("managed_mount_targets", []))
    remote_host_profile = host_profile is not None
    checks: list[dict[str, object]] = []

    workspace_ref = resolved.get("workspace_ref")
    checks.append(
        {
            "kind": "workspace_ref",
            "path": workspace_ref,
            "ok": bool(workspace_ref),
            "reason": None if workspace_ref else "workspace_ref is not configured",
        }
    )

    if mode == "shared_fs":
        for mount in resolved["mounts"]:
            source = _mount_source(mount)
            exists = Path(source).exists()
            checks.append(
                {
                    "kind": "mount_source",
                    "mount": mount,
                    "path": source,
                    "ok": exists,
                    "required_now": not remote_host_profile,
                    "verified_locally": not remote_host_profile,
                    "reason": None
                    if exists
                    else (
                        "path not verified on the operator host for the selected host_profile"
                        if remote_host_profile
                        else "shared_fs source path does not exist"
                    ),
                }
            )
    else:
        git_ok = shutil.which("git") is not None
        checks.append(
            {
                "kind": "binary",
                "name": "git",
                "ok": git_ok,
                "reason": None if git_ok else "git is not installed",
            }
        )
        for mount in resolved["mounts"]:
            source = _mount_source(mount)
            target = _mount_target(mount)
            exists = Path(source).exists()
            required_now = (target not in managed_targets) and not remote_host_profile
            checks.append(
                {
                    "kind": "mount_source",
                    "mount": mount,
                    "path": source,
                    "ok": exists,
                    "required_now": required_now,
                    "verified_locally": not remote_host_profile,
                    "reason": None
                    if exists
                    else (
                        "path not verified on the operator host for the selected host_profile"
                        if remote_host_profile
                        else
                        "path will be created or prepared by deploy script"
                        if not required_now
                        else "required mount source path does not exist"
                    ),
                }
            )

    ok = all(bool(item["ok"]) for item in checks if item.get("required_now", True))
    _emit(
        {
            "ok": ok,
            "workspace": resolved,
            "checks": checks,
        }
    )
    if not ok:
        raise typer.Exit(1)
