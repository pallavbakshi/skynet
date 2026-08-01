"""``skyops upgrade`` — version and migration management."""

from __future__ import annotations

import json

import typer

upgrade_app = typer.Typer(help="Upgrade, rollback, and version management.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@upgrade_app.command("status")
def upgrade_status() -> None:
    """Show current version and migration state."""
    from agp._ops_helpers import get_upgrade_status

    result = get_upgrade_status()
    _emit(result)


@upgrade_app.command("check")
def upgrade_check(
    release_version: str = typer.Option(..., "--release", help="Target release version to check."),
) -> None:
    """Pre-upgrade compatibility check against running runtimes."""
    from sqlalchemy import select

    from agp._ops_helpers import get_upgrade_status
    from agp.db import SessionLocal
    from agp.models import Runtime

    current = get_upgrade_status()
    current_release = current.get("release_version", "0.1.0")

    def _parse(v: str) -> tuple[int, int, int]:
        parts = v.split(".")
        return int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0

    target_major, target_minor, _ = _parse(release_version)
    cur_major, _cur_minor, _ = _parse(current_release)

    issues: list[str] = []
    if target_major != cur_major:
        issues.append(f"Major version change ({current_release} -> {release_version}): cross-major upgrades not supported")

    session = SessionLocal()
    try:
        runtimes = session.scalars(select(Runtime)).all()
        for rt in runtimes:
            rt_major, rt_minor, _ = _parse(rt.release_version)
            if rt_major != target_major:
                issues.append(f"Runtime {rt.runtime_id} (v{rt.release_version}): major version mismatch with target")
            elif target_minor - rt_minor > 1:
                issues.append(f"Runtime {rt.runtime_id} (v{rt.release_version}): will exceed 1-minor skew tolerance")
    finally:
        session.close()

    result = {
        "current_release": current_release,
        "target_release": release_version,
        "runtimes_checked": len(runtimes),
        "issues": issues,
        "compatible": len(issues) == 0,
    }
    _emit(result)
    if issues:
        typer.echo("Pre-upgrade check FAILED:", err=True)
        for issue in issues:
            typer.echo(f"  - {issue}", err=True)
        raise typer.Exit(1)
    typer.echo("Pre-upgrade check passed.")


@upgrade_app.command("apply")
def upgrade_apply(
    schema_version: str = typer.Option(..., "--schema", help="New schema version."),
    release_version: str = typer.Option(..., "--release", help="New release version."),
) -> None:
    """Record a new version (mark upgrade complete)."""
    from agp._ops_helpers import mark_upgrade

    result = mark_upgrade(schema_version=schema_version, release_version=release_version)
    _emit(result)
    typer.echo("Upgrade recorded.")


@upgrade_app.command("rollback")
def upgrade_rollback() -> None:
    """Roll back to the previously recorded version."""
    from agp._ops_helpers import rollback_to_previous_version

    result = rollback_to_previous_version()
    _emit(result)
    typer.echo("Rollback complete.")
