"""``skyops status`` — show service health via port probing."""

from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from urllib.parse import urlparse

import typer

from skyops.config import SkyopsConfig, find_config, load_config

status_app = typer.Typer(help="Show AGP stack service status.")


@dataclass
class ServiceInfo:
    name: str
    port: int | None
    running: bool
    health: str  # "healthy", "unhealthy", "n/a", "stopped"
    uptime: str = "-"


def _probe_tcp(host: str, port: int, timeout: float = 0.5) -> bool:
    """Return True if a TCP connection succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def _probe_http_health(url: str, timeout: float = 2.0) -> bool:
    """Return True if GET url returns 2xx."""
    try:
        import httpx

        resp = httpx.get(url, timeout=timeout)
        return resp.is_success
    except Exception:
        return False


def _parse_port(url: str) -> int | None:
    """Extract port from a URL string."""
    parsed = urlparse(url)
    return parsed.port


def _docker_compose_services(cfg: SkyopsConfig) -> list[ServiceInfo]:
    """Check docker compose service status."""
    compose_file = cfg.stack.compose_file
    project = cfg.stack.project_name
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "-p", project, "ps", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    import json

    services: list[ServiceInfo] = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = entry.get("Service", entry.get("Name", "unknown"))
        state = entry.get("State", "")
        running = state == "running"
        health = entry.get("Health", "")
        if not health:
            health = "healthy" if running else "stopped"
        # Uptime from RunningFor or Status
        uptime = entry.get("RunningFor", entry.get("Status", "-"))
        if not uptime:
            uptime = "-"
        # Try to extract published port
        port = None
        publishers = entry.get("Publishers") or []
        for pub in publishers:
            p = pub.get("PublishedPort")
            if p and p > 0:
                port = p
                break
        services.append(ServiceInfo(name=name, port=port, running=running, health=health, uptime=uptime))
    return services


def _bare_metal_services(cfg: SkyopsConfig) -> list[ServiceInfo]:
    """Detect services by probing known ports."""
    from skyops._client import resolve_host_for_url, resolve_server_url

    db_host = resolve_host_for_url(cfg.database.url)
    redis_host = resolve_host_for_url(cfg.redis.url)
    minio_host = resolve_host_for_url(cfg.s3.endpoint_url)
    cp_url = resolve_server_url(cfg)
    cp_host = resolve_host_for_url(cp_url)

    checks: list[tuple[str, str, int | None]] = [
        ("postgres", db_host, _parse_port(cfg.database.url) or 5432),
        ("redis", redis_host, _parse_port(cfg.redis.url) or 6379),
        ("minio", minio_host, _parse_port(cfg.s3.endpoint_url) or 9000),
        ("control-plane", cp_host, cfg.server.port),
        ("lease-sweeper", "", None),
        ("runtime-sweeper", "", None),
        ("runtime", "", None),
    ]
    if cfg.monitoring.prometheus:
        checks.append(("prometheus", "127.0.0.1", 9090))
    if cfg.monitoring.grafana:
        checks.append(("grafana", "127.0.0.1", 3000))

    services: list[ServiceInfo] = []
    for name, host, port in checks:
        if port is None:
            running = _process_running(name)
            services.append(ServiceInfo(name=name, port=None, running=running, health="n/a" if running else "stopped"))
            continue
        running = _probe_tcp(host, port)
        if running and name == "control-plane":
            healthy = _probe_http_health(f"{cp_url}/health")
            health = "healthy" if healthy else "unhealthy"
        else:
            health = "healthy" if running else "stopped"
        services.append(ServiceInfo(name=name, port=port, running=running, health=health))
    return services


def _process_running(name: str) -> bool:
    """Check if a named agp process is running via pgrep."""
    patterns = {
        "lease-sweeper": "agp.*sweep-loop",
        "runtime-sweeper": "agp.*sweep-runtimes-loop",
        "runtime": "agp.*runtime-work-loop",
    }
    pattern = patterns.get(name, name)
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _platform_summary(cfg: SkyopsConfig) -> list[str]:
    """Try to get platform summary (jobs, agents) from the control plane."""
    lines: list[str] = []
    try:
        from skyops._client import build_client

        with build_client(cfg) as client:
            summary = client.observability_summary()
            total = summary.get("total_jobs", 0)
            active_runs = summary.get("active_runs", 0)
            queued = summary.get("queued_jobs", 0)
            lines.append(f"  Platform:  {total} jobs total, {active_runs} running, {queued} queued")

            agents = client.list_agents(limit=200)
            agent_names = [a["agent_id"] for a in agents.get("items", []) if a.get("status") == "active"]
            lines.append(f"  Agents:    {len(agent_names)} active ({', '.join(agent_names[:5])})")
    except Exception:
        pass
    # Profile path
    profile_path = Path.home() / ".agp" / "profiles" / "default.toml"
    if profile_path.exists():
        lines.append(f"  Profile:   {profile_path}")
    return lines


def _format_table(services: Sequence[ServiceInfo], mode: str, config_path: str, summary_lines: list[str] | None = None) -> str:
    """Format service info as an ASCII table."""
    lines = [
        f"  AGP Stack ({mode} mode)",
        f"  Config: {config_path}",
        "",
        f"  {'SERVICE':<20} {'STATE':<10} {'PORT':<8} {'UPTIME':<14} {'HEALTH'}",
        f"  {'─' * 62}",
    ]
    for svc in services:
        state = "running" if svc.running else "stopped"
        port_str = str(svc.port) if svc.port else "-"
        lines.append(f"  {svc.name:<20} {state:<10} {port_str:<8} {svc.uptime:<14} {svc.health}")

    if summary_lines:
        lines.append("")
        lines.extend(summary_lines)
    return "\n".join(lines)


@status_app.callback(invoke_without_command=True)
def status(ctx: typer.Context) -> None:
    """Show service status for the AGP stack."""
    if ctx.invoked_subcommand is not None:
        return

    try:
        cfg = load_config()
    except FileNotFoundError:
        typer.echo("skyops.toml not found. Run `skyops init` first.", err=True)
        raise typer.Exit(1)

    config_path = str(cfg._config_path or "?")
    mode = cfg.stack.mode

    if mode == "docker":
        services = _docker_compose_services(cfg)
        if not services:
            # Fallback to port probing if compose ps fails
            services = _bare_metal_services(cfg)
    else:
        services = _bare_metal_services(cfg)

    # Get platform summary if control plane is running
    summary_lines = _platform_summary(cfg)

    typer.echo("")
    typer.echo(_format_table(services, mode, config_path, summary_lines))
    typer.echo("")
