"""``skyops metrics``, ``skyops alerts``, ``skyops trace``, ``skyops logs`` — monitoring commands."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import typer

from skyops._client import build_client
from skyops.config import load_config

monitor_app = typer.Typer(help="Monitoring and observability commands.")
logs_app = typer.Typer(help="Log viewing commands.")


class DockerCommandTimeout(RuntimeError):
    """Raised when Docker inspection commands time out."""


def _client():
    return build_client()


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _run_output(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=10)


def _docker_runtime_container(project: str, compose_file: str, runtime_id: str, runtime_hostname: str | None = None) -> str | None:
    try:
        result = _run_output("docker", "compose", "-f", compose_file, "-p", project, "ps", "-q")
    except subprocess.TimeoutExpired:
        raise DockerCommandTimeout("docker compose ps timed out") from None
    if result.returncode != 0:
        return None
    container_ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for container_id in container_ids:
        try:
            inspect = _run_output("docker", "inspect", container_id)
        except subprocess.TimeoutExpired:
            raise DockerCommandTimeout(f"docker inspect timed out for container {container_id}") from None
        if inspect.returncode != 0:
            continue
        try:
            payload = json.loads(inspect.stdout)
        except json.JSONDecodeError:
            continue
        if not payload:
            continue
        data = payload[0]
        env = data.get("Config", {}).get("Env", []) or []
        env_map: dict[str, str] = {}
        for item in env:
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            env_map[key] = value
        if env_map.get("AGP_RUNTIME_ID") == runtime_id:
            return container_id
        if runtime_hostname and (
            env_map.get("AGP_RUNTIME_HOSTNAME") == runtime_hostname
            or data.get("Config", {}).get("Hostname") == runtime_hostname
            or data.get("Name", "").lstrip("/") == runtime_hostname
        ):
            return container_id
    return None


def _run_streaming(cmd: list[str]) -> None:
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise typer.Exit(result.returncode or 1)


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
            result = client.ops_health()
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


@monitor_app.command("events")
def events(
    job_id: str = typer.Argument(help="Job ID to show raw events for."),
    limit: int = typer.Option(200, "--limit", "-l", help="Max events per page."),
) -> None:
    """Show the raw ordered event stream for a job."""
    items: list[dict] = []
    cursor: str | None = None
    with _client() as client:
        while True:
            page = client.get_job_events(job_id, limit=limit, cursor=cursor)
            items.extend(page.get("items", []))
            cursor = page.get("page", {}).get("next_cursor")
            if not cursor:
                break
    _emit({"job_id": job_id, "items": items, "count": len(items)})


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
        _run_streaming(cmd)
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
        runtime_data: dict[str, Any] | None = None
        try:
            with _client() as client:
                runtime_data = client.get_runtime(runtime_id)
        except Exception:
            runtime_data = None
        try:
            container_id = _docker_runtime_container(
                cfg.stack.project_name,
                cfg.stack.compose_file,
                runtime_id,
                runtime_hostname=runtime_data.get("hostname") if runtime_data else None,
            )
        except DockerCommandTimeout as exc:
            typer.echo(f"Docker lookup timed out while resolving runtime {runtime_id}: {exc}", err=True)
            raise typer.Exit(1) from exc
        if container_id is None:
            typer.echo(
                f"Could not map runtime {runtime_id} to a Docker container in compose project {cfg.stack.project_name}.",
                err=True,
            )
            raise typer.Exit(1)
        cmd = ["docker", "logs", "--follow", container_id]
        _run_streaming(cmd)
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
        _run_streaming(cmd)
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
        _run_streaming(cmd)


@logs_app.command("prune")
def logs_prune() -> None:
    """Prune old rotated log files."""
    from agp._ops_helpers import prune_observability_logs

    result = prune_observability_logs()
    _emit(result)
