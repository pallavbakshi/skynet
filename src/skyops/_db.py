"""``skyops db`` — database and seeding commands."""

from __future__ import annotations

import subprocess

import typer

from skyops.config import SkyopsConfig, load_config

db_app = typer.Typer(help="Database and data seeding commands.")


@db_app.command("init")
def db_init() -> None:
    """Create database schema (runs ``agp initdb``)."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        cmd = _compose_exec(cfg) + ["control-plane", "agp", "initdb"]
        typer.echo(f"Running: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)
    else:
        typer.echo("Running: agp initdb")
        subprocess.run(["agp", "initdb"], check=True)
    typer.echo("Database initialized.")


@db_app.command("seed")
def db_seed() -> None:
    """Seed capabilities and agents from skyops.toml configuration.

    Replaces ``scripts/bootstrap_local_stack.py``.
    """
    cfg = load_config()

    from skyops._client import build_client

    with build_client(cfg) as client:
        # Wait for health
        import time

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                client.health()
                break
            except Exception:
                time.sleep(1.0)
        else:
            raise RuntimeError("Control plane did not become healthy before seed timeout")

        # Seed capabilities via the control-plane admin API.
        for cap_id, cap_data in cfg.capabilities.items():
            client.seed_capability(
                cap_id,
                cap_data.get("name", cap_id),
                version=cap_data.get("version", "v1"),
                image_ref=cap_data.get("image_ref", ""),
                model_ref=cap_data.get("model_ref", ""),
            )
            typer.echo(f"  Seeded capability: {cap_id}")

        # Seed agents via SDK
        for agent_id, agent_data in cfg.agents.items():
            cap_id = agent_data.get("capability_id", "")
            workspace = cfg.resolve_agent_workspace(agent_id).get("workspace_ref")
            agents = client.list_agents(capability_id=cap_id, limit=200)
            existing = next(
                (item for item in agents["items"] if item["agent_id"] == agent_id),
                None,
            )
            if existing is not None:
                # Patch workspace_ref if changed
                if workspace and existing.get("workspace_ref") != workspace:
                    client.patch_agent(agent_id, workspace_ref=workspace)
                    typer.echo(f"  Updated agent: {agent_id} (workspace_ref)")
                else:
                    typer.echo(f"  Agent already exists: {agent_id}")
                continue
            client.register_agent(
                agent_id, cap_id,
                workspace_ref=workspace,
            )
            typer.echo(f"  Seeded agent: {agent_id}")

    typer.echo("Seeding complete.")


@db_app.command("migrate")
def db_migrate() -> None:
    """Run pending database migrations."""
    from agp.migrations import apply_migrations

    result = apply_migrations()
    if result["applied"]:
        for tag in result["applied"]:
            typer.echo(f"  Applied: {tag}")
    else:
        typer.echo("No pending migrations.")
    typer.echo(f"Current version: {result['current_version']}")


@db_app.command("status")
def db_status() -> None:
    """Show database connection health and basic stats."""
    cfg = load_config()

    from agp.db import SessionLocal, engine
    from sqlalchemy import text

    typer.echo(f"Database URL: {cfg.database.url}")
    try:
        session = SessionLocal()
        try:
            # Check connection
            session.execute(text("SELECT 1"))
            typer.echo("Connection: OK")

            # Schema version
            row = session.execute(
                text("SELECT value FROM system_metadata WHERE key = 'schema_version'")
            ).first()
            if row:
                typer.echo(f"Schema version: {row[0]}")

            # Table counts
            for table in ["capabilities", "agents", "jobs", "deliveries", "artifacts"]:
                try:
                    count = session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                    typer.echo(f"  {table}: {count} rows")
                except Exception:
                    typer.echo(f"  {table}: (table not found)")

        finally:
            session.close()
    except Exception as e:
        typer.echo(f"Connection: FAILED ({e})", err=True)
        raise typer.Exit(1)


def _compose_exec(cfg: SkyopsConfig) -> list[str]:
    return [
        "docker", "compose",
        "-f", cfg.stack.compose_file,
        "-p", cfg.stack.project_name,
        "exec",
    ]
