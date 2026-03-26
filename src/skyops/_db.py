"""``skyops db`` — database and seeding commands."""

from __future__ import annotations

import json
import subprocess
import sys

import typer

from skyops.config import SkyopsConfig, build_agp_env, load_config

db_app = typer.Typer(help="Database and data seeding commands.")

_MIGRATE_SNIPPET = (
    "import json; "
    "from agp.migrations import apply_migrations; "
    "print(json.dumps(apply_migrations()))"
)

_STATUS_SNIPPET = """
import json
import os

from sqlalchemy import create_engine, text

engine = create_engine(os.environ["AGP_DATABASE_URL"], future=True)
payload = {"connection": "OK", "schema_version": None, "counts": {}}
tables = ["capabilities", "agents", "jobs", "deliveries", "artifacts"]

with engine.begin() as conn:
    conn.execute(text("SELECT 1"))
    try:
        row = conn.execute(
            text("SELECT value FROM system_metadata WHERE key = 'schema_version'")
        ).first()
    except Exception:
        row = None
    payload["schema_version"] = row[0] if row else None
    for table in tables:
        try:
            # table names come from the hardcoded list above, not user input
            payload["counts"][table] = conn.execute(
                text('SELECT count(*) FROM "' + table + '"')
            ).scalar()
        except Exception:
            payload["counts"][table] = None

print(json.dumps(payload))
""".strip()


def _python_json(cfg: SkyopsConfig, code: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env=build_agp_env(cfg),
    )
    return json.loads(result.stdout)


@db_app.command("init")
def db_init() -> None:
    """Create database schema (runs ``agp initdb``)."""
    cfg = load_config()
    try:
        if cfg.stack.mode == "docker":
            cmd = _compose_exec(cfg) + ["control-plane", "agp", "initdb"]
            typer.echo(f"Running: {' '.join(cmd)}")
            subprocess.run(cmd, check=True, timeout=30)
        else:
            typer.echo("Running: agp initdb")
            subprocess.run(["agp", "initdb"], check=True, env=build_agp_env(cfg), timeout=30)
    except subprocess.TimeoutExpired as exc:
        typer.echo(f"Database init timed out: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo("Database initialized.")


@db_app.command("seed")
def db_seed() -> None:
    """Seed capabilities and agents from skyops.toml configuration.

    Replaces ``scripts/bootstrap_local_stack.py``.
    """
    cfg = load_config()

    from skyops._client import build_client

    with build_client(cfg) as client:
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

        for cap_id, cap_data in cfg.capabilities.items():
            client.seed_capability(
                cap_id,
                cap_data.get("name", cap_id),
                version=cap_data.get("version", "v1"),
                image_ref=cap_data.get("image_ref", ""),
                model_ref=cap_data.get("model_ref", ""),
            )
            typer.echo(f"  Seeded capability: {cap_id}")

        for agent_id, agent_data in cfg.agents.items():
            cap_id = agent_data.get("capability_id", "")
            workspace = cfg.resolve_agent_workspace_ref(agent_id)
            agents = client.list_agents(capability=cap_id, limit=200)
            existing = next(
                (item for item in agents["items"] if item["agent_id"] == agent_id),
                None,
            )
            if existing is not None:
                if existing.get("workspace_ref") != workspace:
                    client.patch_agent(agent_id, workspace_ref=workspace)
                    typer.echo(f"  Updated agent: {agent_id} (workspace_ref)")
                else:
                    typer.echo(f"  Agent already exists: {agent_id}")
                continue
            client.register_agent(
                agent_id,
                cap_id,
                workspace_ref=workspace,
            )
            typer.echo(f"  Seeded agent: {agent_id}")

    typer.echo("Seeding complete.")


@db_app.command("migrate")
def db_migrate() -> None:
    """Run pending database migrations."""
    cfg = load_config()
    try:
        if cfg.stack.mode == "docker":
            completed = subprocess.run(
                _compose_exec(cfg) + ["control-plane", "python", "-c", _MIGRATE_SNIPPET],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            result = json.loads(completed.stdout)
        else:
            result = _python_json(cfg, _MIGRATE_SNIPPET)
    except subprocess.TimeoutExpired as exc:
        typer.echo(f"Database migrate timed out: {exc}", err=True)
        raise typer.Exit(1) from exc
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

    typer.echo(f"Database URL: {cfg.database.url}")
    try:
        if cfg.stack.mode == "docker":
            completed = subprocess.run(
                _compose_exec(cfg) + ["control-plane", "python", "-c", _STATUS_SNIPPET],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            payload = json.loads(completed.stdout)
        else:
            payload = _python_json(cfg, _STATUS_SNIPPET)

        typer.echo(f"Connection: {payload['connection']}")
        if payload.get("schema_version"):
            typer.echo(f"Schema version: {payload['schema_version']}")
        for table, count in payload.get("counts", {}).items():
            if count is None:
                typer.echo(f"  {table}: (table not found)")
            else:
                typer.echo(f"  {table}: {count} rows")
    except subprocess.TimeoutExpired as e:
        typer.echo(f"Database status timed out: {e}", err=True)
        raise typer.Exit(1)
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
