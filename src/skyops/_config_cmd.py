"""``skyops config show`` and ``skyops config set`` commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

from skyops.config import find_config, load_config

config_app = typer.Typer(help="Show or update skyops configuration.")


@config_app.command("show")
def config_show(
    unmask: bool = typer.Option(False, "--unmask", help="Show secret values unmasked."),
) -> None:
    """Print the current merged configuration with secrets masked."""
    try:
        cfg = load_config()
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e

    display = cfg.to_display_dict(mask_secrets=not unmask)
    source = str(cfg._config_path) if cfg._config_path else "(defaults)"
    typer.echo(f"# Source: {source}\n")
    # Pretty-print as indented key = value groups
    for section, values in display.items():
        if isinstance(values, dict):
            typer.echo(f"[{section}]")
            for k, v in values.items():
                if isinstance(v, dict):
                    typer.echo(f"  [{section}.{k}]")
                    for kk, vv in v.items():
                        typer.echo(f"    {kk} = {_fmt(vv)}")
                else:
                    typer.echo(f"  {k} = {_fmt(v)}")
            typer.echo("")
        else:
            typer.echo(f"{section} = {_fmt(values)}")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(help="Dotted key path, e.g. 'server.port' or 's3.bucket'."),
    value: str = typer.Argument(help="New value to set."),
) -> None:
    """Set a value in skyops.local.toml (never modifies base skyops.toml)."""
    config_path = find_config()
    if config_path is None:
        typer.echo("skyops.toml not found. Run `skyops init` first.", err=True)
        raise typer.Exit(1)

    local_path = config_path.parent / "skyops.local.toml"

    # Load existing local overrides
    existing: dict = {}
    if local_path.is_file():
        with open(local_path, "rb") as f:
            existing = tomllib.load(f)

    # Set the dotted key
    parts = key.split(".")
    target = existing
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = _coerce(value)

    # Write back (simple TOML serialization)
    _write_toml(local_path, existing)
    typer.echo(f"Set {key} = {_fmt(_coerce(value))} in {local_path}")


def _coerce(value: str) -> str | int | float | bool:
    """Attempt to coerce a CLI string to the right type."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return str(v)


def _write_toml(path: Path, data: dict) -> None:
    """Write a simple nested dict as TOML."""
    lines: list[str] = [
        "# skyops.local.toml — local overrides (gitignored)",
        "# Managed by `skyops config set`. Safe to edit by hand.",
        "",
    ]
    _write_section(lines, data, prefix="")
    path.write_text("\n".join(lines) + "\n")


def _write_section(lines: list[str], data: dict, prefix: str) -> None:
    # First pass: write simple key-value pairs
    for k, v in data.items():
        if not isinstance(v, dict):
            lines.append(f"{k} = {_fmt(v)}")

    # Second pass: write sub-tables
    for k, v in data.items():
        if isinstance(v, dict):
            section = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
            lines.append(f"\n[{section}]")
            _write_section(lines, v, prefix=section)
