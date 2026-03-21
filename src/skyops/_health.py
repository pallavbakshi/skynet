"""``skyops health`` — deep health check across all stack components."""

from __future__ import annotations

from urllib.parse import urlparse

import typer

from skyops.config import load_config
from skyops._client import resolve_server_url, resolve_host_for_url
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

    # ── Database ──────────────────────────────────────────────────
    db_host = resolve_host_for_url(cfg.database.url)
    db_port = _port_from_url(cfg.database.url) or 5432
    db_ok = _probe_tcp(db_host, db_port)
    checks.append(("postgres", db_ok, f"{db_host}:{db_port}"))

    # ── Redis ─────────────────────────────────────────────────────
    redis_host = resolve_host_for_url(cfg.redis.url)
    redis_port = _port_from_url(cfg.redis.url) or 6379
    redis_tcp = _probe_tcp(redis_host, redis_port)
    if redis_tcp:
        redis_ok = _redis_ping(cfg.redis.url)
        checks.append(("redis", redis_ok, f"{redis_host}:{redis_port} PING"))
    else:
        checks.append(("redis", False, f"{redis_host}:{redis_port}"))

    # ── MinIO ─────────────────────────────────────────────────────
    minio_host = resolve_host_for_url(cfg.s3.endpoint_url)
    minio_port = _port_from_url(cfg.s3.endpoint_url) or 9000
    minio_tcp = _probe_tcp(minio_host, minio_port)
    if minio_tcp:
        minio_ok = _minio_bucket_access(cfg)
        checks.append(("minio", minio_ok, f"{minio_host}:{minio_port} bucket={cfg.s3.bucket}"))
    else:
        checks.append(("minio", False, f"{minio_host}:{minio_port}"))

    # ── Control plane ─────────────────────────────────────────────
    cp_url = resolve_server_url(cfg)
    cp_host = resolve_host_for_url(cp_url)
    cp_port = cfg.server.port
    cp_tcp = _probe_tcp(cp_host, cp_port)
    cp_health = _probe_http_health(f"{cp_url}/health") if cp_tcp else False
    checks.append(("control-plane", cp_health, f"{cp_host}:{cp_port} /health"))

    # ── API + runtime heartbeats ──────────────────────────────────
    if cp_health:
        try:
            from skyops._client import build_client

            with build_client(cfg) as client:
                summary = client.observability_summary()
                jobs_total = summary.get("total_jobs", "?")
                agents_active = summary.get("active_agents", "?")
                checks.append(("  api", True, f"jobs={jobs_total} agents={agents_active}"))

                agents = client.list_agents(limit=200)
                stale_runtimes: list[str] = []
                for agent in agents.get("items", []):
                    rt_id = agent.get("assigned_runtime_id")
                    if rt_id and agent.get("health_status") in ("degraded", "unreachable"):
                        stale_runtimes.append(rt_id)
                if stale_runtimes:
                    checks.append(("  runtimes", False, f"stale heartbeats: {', '.join(stale_runtimes)}"))
                else:
                    checks.append(("  runtimes", True, "heartbeats OK"))
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


def _redis_ping(redis_url: str) -> bool:
    try:
        import redis
        r = redis.from_url(redis_url, socket_timeout=2.0)
        return r.ping()
    except Exception:
        return False


def _minio_bucket_access(cfg) -> bool:
    try:
        import boto3
        from botocore.config import Config as BotoConfig
        s3 = boto3.client(
            "s3",
            endpoint_url=cfg.s3.endpoint_url,
            aws_access_key_id=cfg.s3.access_key_id,
            aws_secret_access_key=cfg.s3.secret_access_key,
            region_name="us-east-1",
            config=BotoConfig(signature_version="s3v4"),
        )
        s3.head_bucket(Bucket=cfg.s3.bucket)
        return True
    except Exception:
        return False
