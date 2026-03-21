"""``skyops metrics``, ``skyops alerts``, ``skyops trace``, ``skyops logs`` — monitoring commands."""

from __future__ import annotations

import json
import subprocess

import typer

from skyops.config import load_config

monitor_app = typer.Typer(help="Monitoring and observability commands.")
logs_app = typer.Typer(help="Log viewing commands.")


def _client():
    from agp.client import AgpClient, AgpProfile

    cfg = load_config()
    profile = AgpProfile(
        server_url=f"http://127.0.0.1:{cfg.server.port}",
        token=cfg.security.operator_token or None,
    )
    return AgpClient(profile=profile)


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@monitor_app.command("metrics")
def metrics(
    prometheus: bool = typer.Option(False, "--prometheus", help="Raw Prometheus text format."),
) -> None:
    """Show observability summary or raw Prometheus metrics."""
    with _client() as client:
        if prometheus:
            result = client.observability_metrics()
            typer.echo(result)
        else:
            result = client.observability_summary()
            _emit(result)


@monitor_app.command("alerts")
def alerts(
    dispatch: bool = typer.Option(False, "--dispatch", help="Show dispatch alerts only."),
) -> None:
    """Show current active alerts."""
    with _client() as client:
        if dispatch:
            result = client.observability_dispatch_alerts()
        else:
            result = client.observability_alerts()
    _emit(result)


@monitor_app.command("trace")
def trace(
    job_id: str = typer.Argument(help="Job ID to trace."),
) -> None:
    """Show execution trace for a job."""
    with _client() as client:
        result = client.job_trace(job_id)
    _emit(result)


@logs_app.command("control-plane")
def logs_control_plane(
    limit: int = typer.Option(100, "--limit", "-l", help="Max log entries."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow logs (docker mode)."),
) -> None:
    """Show control plane logs."""
    cfg = load_config()
    if follow and cfg.stack.mode == "docker":
        # Tail docker compose logs
        cmd = [
            "docker", "compose",
            "-f", cfg.stack.compose_file,
            "-p", cfg.stack.project_name,
            "logs", "--follow", "control-plane",
        ]
        subprocess.run(cmd)
        return

    with _client() as client:
        result = client.logs_control_plane(limit=limit)
    _emit(result)


@logs_app.command("runtime")
def logs_runtime(
    runtime_id: str = typer.Argument(help="Runtime ID."),
    limit: int = typer.Option(100, "--limit", "-l", help="Max log entries."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow logs (docker mode)."),
) -> None:
    """Show runtime logs."""
    cfg = load_config()
    if follow and cfg.stack.mode == "docker":
        cmd = [
            "docker", "compose",
            "-f", cfg.stack.compose_file,
            "-p", cfg.stack.project_name,
            "logs", "--follow", "runtime",
        ]
        subprocess.run(cmd)
        return

    with _client() as client:
        result = client.logs_runtime(runtime_id, limit=limit)
    _emit(result)


@logs_app.command("service")
def logs_service(
    service: str = typer.Argument(help="Service name (e.g. control-plane, runtime, postgres)."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Follow logs."),
    tail: int = typer.Option(100, "--tail", "-n", help="Number of lines to show."),
) -> None:
    """Tail logs for any service. Docker: compose logs. Bare-metal: JSONL files."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        cmd = [
            "docker", "compose",
            "-f", cfg.stack.compose_file,
            "-p", cfg.stack.project_name,
            "logs", "--tail", str(tail),
        ]
        if follow:
            cmd.append("--follow")
        cmd.append(service)
        subprocess.run(cmd)
    else:
        # Bare-metal: try to tail JSONL log files
        from agp.config import settings

        log_file = settings.log_root / f"{service}.jsonl"
        if not log_file.exists():
            typer.echo(f"Log file not found: {log_file}", err=True)
            raise typer.Exit(1)
        cmd = ["tail", "-n", str(tail)]
        if follow:
            cmd.append("-f")
        cmd.append(str(log_file))
        subprocess.run(cmd)


@logs_app.command("prune")
def logs_prune() -> None:
    """Prune old rotated log files."""
    from agp._ops_helpers import prune_observability_logs

    result = prune_observability_logs()
    _emit(result)
