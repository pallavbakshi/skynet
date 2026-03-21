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

    from agp.client import AgpClient, AgpProfile

    profile = AgpProfile(
        server_url=f"http://127.0.0.1:{cfg.server.port}",
        token=cfg.security.operator_token or None,
    )

    with AgpClient(profile=profile) as client:
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

        # Seed capabilities via direct DB (same as bootstrap_local_stack.py)
        from agp.db import SessionLocal, init_db
        from agp.models import Capability, CapabilityPool, utc_now

        session = SessionLocal()
        try:
            for cap_id, cap_data in cfg.capabilities.items():
                if session.get(Capability, cap_id) is None:
                    now = utc_now()
                    session.add(
                        Capability(
                            capability_id=cap_id,
                            name=cap_data.get("name", cap_id),
                            version="v1",
                            image_ref=cap_data.get("image_ref", ""),
                            model_ref=cap_data.get("model_ref", ""),
                            resource_tier="small",
                            permission_profile="default",
                            queue_mode="agent",
                            runtime_requirements_json={},
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.flush()
                    if session.get(CapabilityPool, cap_id) is None:
                        session.add(
                            CapabilityPool(
                                capability_id=cap_id,
                                queue_id=f"capability:{cap_id}:v1",
                                routing_policy="least_recent",
                            )
                        )
                    typer.echo(f"  Seeded capability: {cap_id}")
            session.commit()
        finally:
            session.close()

        # Seed agents via API
        for agent_id, agent_data in cfg.agents.items():
            cap_id = agent_data.get("capability_id", "")
            agents = client.list_agents(capability_id=cap_id, limit=200)
            if any(item["agent_id"] == agent_id for item in agents["items"]):
                typer.echo(f"  Agent already exists: {agent_id}")
                continue
            response = client._client.post(
                "/agents/up",
                json={
                    "agent_id": agent_id,
                    "capability_id": cap_id,
                },
            )
            response.raise_for_status()
            typer.echo(f"  Seeded agent: {agent_id}")

    typer.echo("Seeding complete.")


@db_app.command("migrate")
def db_migrate() -> None:
    """Run pending database migrations (placeholder)."""
    typer.echo("No pending migrations. Schema is up to date.")
    typer.echo("(Migration framework not yet implemented — this is a placeholder.)")


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
