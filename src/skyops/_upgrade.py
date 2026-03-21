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
