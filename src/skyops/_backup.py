"""``skyops backup`` — backup and disaster recovery commands."""

from __future__ import annotations

import json

import typer

backup_app = typer.Typer(help="Backup, restore, and disaster recovery.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@backup_app.command("create")
def backup_create(
    path: str = typer.Argument("./backups/latest", help="Directory to write backup into."),
) -> None:
    """Create a backup snapshot."""
    from agp._ops_helpers import create_backup_snapshot

    result = create_backup_snapshot(backup_dir=path)
    _emit(result)
    typer.echo(f"Backup created at {path}")


@backup_app.command("restore")
def backup_restore(
    path: str = typer.Argument(help="Directory to restore from."),
) -> None:
    """Restore from a backup snapshot."""
    from agp._ops_helpers import restore_backup_snapshot

    result = restore_backup_snapshot(backup_dir=path)
    _emit(result)
    typer.echo("Restore complete.")


@backup_app.command("list")
def backup_list(
    path: str = typer.Argument("./backups", help="Directory containing backup snapshots."),
) -> None:
    """List available backup snapshots."""
    from pathlib import Path

    backup_dir = Path(path)
    if not backup_dir.is_dir():
        typer.echo(f"Backup directory not found: {path}", err=True)
        raise typer.Exit(1)

    snapshots: list[dict] = []
    for manifest_path in sorted(backup_dir.rglob("manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            snapshots.append({
                "path": str(manifest_path.parent),
                "database_url": manifest.get("database_url", "?"),
                "artifact_backend": manifest.get("artifact_backend", "?"),
            })
        except Exception:
            continue

    if not snapshots:
        typer.echo("No backup snapshots found.")
        return

    typer.echo(f"Found {len(snapshots)} backup(s):")
    for snap in snapshots:
        typer.echo(f"  {snap['path']}")
    _emit(snapshots)


@backup_app.command("validate")
def backup_validate(
    limit: int | None = typer.Option(None, "--limit", help="Max artifacts to check."),
) -> None:
    """Verify artifact references resolve after a restore."""
    from agp._ops_helpers import validate_restored_state

    result = validate_restored_state(limit=limit)
    _emit(result)
    if not result["ok"]:
        typer.echo("Validation FAILED: some artifacts are missing.", err=True)
        raise typer.Exit(1)
    typer.echo("Validation passed.")


@backup_app.command("recover")
def backup_recover(
    path: str = typer.Argument(help="Directory to restore from."),
    validate_limit: int | None = typer.Option(None, "--validate-limit", help="Max artifacts to validate."),
) -> None:
    """Full restore + validation + queue reconstruction."""
    from agp._ops_helpers import restore_and_recover_snapshot

    result = restore_and_recover_snapshot(backup_dir=path, validate_limit=validate_limit)
    _emit(result)
    if not result["ok"]:
        typer.echo("Recovery validation FAILED.", err=True)
        raise typer.Exit(1)
    typer.echo("Recovery complete.")
