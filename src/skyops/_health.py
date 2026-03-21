"""``skyops health`` — deep health check across all stack components."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import typer

from skyops.config import load_config
from skyops._status import _probe_tcp, _probe_http_health

health_app = typer.Typer(help="Deep health check.")


@health_app.callback(invoke_without_command=True)
def health(ctx: typer.Context) -> None:
    """Check health of all stack components."""
    if ctx.invoked_subcommand is not None:
        return

    cfg = load_config()
    all_ok = True

    checks: list[tuple[str, bool, str]] = []

    # Database
    db_port = _port_from_url(cfg.database.url) or 5432
    db_ok = _probe_tcp("127.0.0.1", db_port)
    checks.append(("postgres", db_ok, f":{db_port}"))

    # Redis
    redis_port = _port_from_url(cfg.redis.url) or 6379
    redis_ok = _probe_tcp("127.0.0.1", redis_port)
    checks.append(("redis", redis_ok, f":{redis_port}"))

    # MinIO
    minio_port = _port_from_url(cfg.s3.endpoint_url) or 9000
    minio_ok = _probe_tcp("127.0.0.1", minio_port)
    checks.append(("minio", minio_ok, f":{minio_port}"))

    # Control plane
    cp_port = cfg.server.port
    cp_tcp = _probe_tcp("127.0.0.1", cp_port)
    cp_health = _probe_http_health(f"http://127.0.0.1:{cp_port}/health") if cp_tcp else False
    checks.append(("control-plane", cp_health, f":{cp_port} /health"))

    # Control plane API (deeper check via AgpClient)
    if cp_health:
        try:
            from skyops._client import build_client

            with build_client(cfg) as client:
                summary = client.observability_summary()
                jobs_total = summary.get("total_jobs", "?")
                agents_active = summary.get("active_agents", "?")
                checks.append(("  api", True, f"jobs={jobs_total} agents={agents_active}"))
        except Exception as e:
            checks.append(("  api", False, str(e)))

    typer.echo("")
    typer.echo(f"  Health Check ({cfg.stack.mode} mode)")
    typer.echo(f"  {'─' * 50}")
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        typer.echo(f"  {name:<20} {status:<6} {detail}")
        if not ok:
            all_ok = False
    typer.echo("")

    if not all_ok:
        typer.echo("Some checks failed.")
        raise typer.Exit(1)
    typer.echo("All checks passed.")


def _port_from_url(url: str) -> int | None:
    parsed = urlparse(url)
    return parsed.port
