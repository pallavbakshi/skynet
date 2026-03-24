"""``skyops runtime`` — runtime debugging commands."""

from __future__ import annotations

import json
import os
import socket

import typer

from skyops.config import load_config
from skyops._client import build_profile, build_client

runtime_debug_app = typer.Typer(help="Runtime debugging commands.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _runtime_bearer_token(cfg) -> str | None:
    token = os.environ.get("AGP_RUNTIME_BEARER_TOKEN")
    if token:
        return token
    return cfg.security.runtime_token or None


@runtime_debug_app.command("list")
def runtime_list(
    status: str | None = typer.Option(None, "--status", help="Filter by runtime status."),
    health_status: str | None = typer.Option(None, "--health", help="Filter by runtime health status."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results."),
    cursor: str | None = typer.Option(None, "--cursor", help="Pagination cursor."),
) -> None:
    """List runtimes known to the control plane."""
    with build_client() as client:
        result = client.list_runtimes(status=status, health_status=health_status, limit=limit, cursor=cursor)
    _emit(result)


@runtime_debug_app.command("inspect")
def runtime_inspect(
    runtime_id: str = typer.Argument(help="Runtime ID to inspect."),
) -> None:
    """Show detailed runtime state."""
    with build_client() as client:
        result = client.get_runtime(runtime_id)
    if result is None:
        typer.echo(f"Runtime not found: {runtime_id}", err=True)
        raise typer.Exit(1)
    _emit(result)


@runtime_debug_app.command("register")
def runtime_register(
    runtime_id: str = typer.Argument(help="Runtime ID to register."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname (defaults to system hostname)."),
) -> None:
    """Register a runtime with the control plane (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity

    cfg = load_config()
    actual_hostname = hostname or socket.gethostname()
    runtime_token = _runtime_bearer_token(cfg)
    server_url = build_profile(cfg).server_url
    identity = RuntimeIdentity(
        runtime_id=runtime_id,
        hostname=actual_hostname,
        server_url=server_url,
        token=runtime_token,
    )
    client = RuntimeClient(identity)
    try:
        result = client.register()
        _emit(result)
    finally:
        client.close()


@runtime_debug_app.command("claim")
def runtime_claim(
    runtime_id: str = typer.Argument(help="Runtime ID."),
    agent_id: str = typer.Argument(help="Agent ID to claim work for."),
    lease_ttl: int = typer.Option(30, "--lease-ttl", help="Lease TTL in seconds."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname."),
) -> None:
    """Claim a single job for a runtime (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity

    cfg = load_config()
    actual_hostname = hostname or socket.gethostname()
    runtime_token = _runtime_bearer_token(cfg)
    server_url = build_profile(cfg).server_url
    identity = RuntimeIdentity(
        runtime_id=runtime_id,
        hostname=actual_hostname,
        server_url=server_url,
        token=runtime_token,
    )
    client = RuntimeClient(identity)
    try:
        result = client.claim(agent_id=agent_id, lease_ttl_seconds=lease_ttl)
        _emit(result)
    finally:
        client.close()


@runtime_debug_app.command("work-once")
def runtime_work_once(
    runtime_id: str = typer.Argument(help="Runtime ID."),
    agent_id: str | None = typer.Option(None, "--agent-id", help="Agent ID."),
    capability_id: str | None = typer.Option(None, "--capability-id", help="Capability ID."),
    server_url: str | None = typer.Option(None, "--server-url", help="Server URL (defaults to config)."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname."),
    artifact_root: str = typer.Option(".agp-artifacts", "--artifact-root", help="Artifact root."),
    host_kind: str = typer.Option("inprocess", "--host-kind", help="Terminal host kind."),
    adapter_kind: str = typer.Option("default", "--adapter-kind", help="Agent adapter kind."),
) -> None:
    """Run a single iteration of the runtime work loop (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity
    from agp.plugins import build_terminal_host, build_agent_adapter
    from agp.runtime import RuntimeSupervisor

    cfg = load_config()
    resolved_url = server_url or build_profile(cfg).server_url
    actual_hostname = hostname or socket.gethostname()
    runtime_token = _runtime_bearer_token(cfg)
    client = RuntimeClient(
        RuntimeIdentity(
            runtime_id=runtime_id,
            hostname=actual_hostname,
            server_url=resolved_url,
            token=runtime_token,
        )
    )
    worker = RuntimeSupervisor(
        client,
        host=build_terminal_host(host_kind),
        adapter=build_agent_adapter(adapter_kind),
        artifact_root=artifact_root,
    )
    from threading import Event

    stop_event = Event()
    try:
        payload = worker.run_forever(
            agent_id=agent_id,
            capability_id=capability_id,
            idle_sleep_seconds=0.25,
            max_iterations=1,
            stop_event=stop_event,
        )
    finally:
        stop_event.set()
        client.close()
    _emit(payload)


@runtime_debug_app.command("work-loop")
def runtime_work_loop(
    runtime_id: str = typer.Argument(help="Runtime ID."),
    server_url: str | None = typer.Option(None, "--server-url", help="Server URL (defaults to config)."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname."),
    agent_id: str | None = typer.Option(None, "--agent-id", help="Agent ID."),
    capability_id: str | None = typer.Option(None, "--capability-id", help="Capability ID."),
    artifact_root: str = typer.Option(".agp-artifacts", "--artifact-root", help="Artifact root."),
    idle_sleep_seconds: float = typer.Option(0.25, "--idle-sleep-seconds", help="Idle poll sleep."),
    max_iterations: int | None = typer.Option(None, "--max-iterations", help="Stop after N iterations."),
    max_local_recoveries: int = typer.Option(1, "--max-local-recoveries", help="Max local recovery attempts."),
    host_kind: str | None = typer.Option(None, "--host-kind", help="Terminal host kind."),
    adapter_kind: str | None = typer.Option(None, "--adapter-kind", help="Agent adapter kind."),
) -> None:
    """Run the continuous runtime work loop."""
    from agp.cli import runtime_work_loop as agp_runtime_work_loop

    cfg = load_config()
    resolved_url = server_url or build_profile(cfg).server_url
    had_previous_token = "AGP_RUNTIME_BEARER_TOKEN" in os.environ
    previous_token = os.environ.get("AGP_RUNTIME_BEARER_TOKEN")
    configured_token = _runtime_bearer_token(cfg)
    if not previous_token and configured_token:
        os.environ["AGP_RUNTIME_BEARER_TOKEN"] = configured_token
    try:
        agp_runtime_work_loop(
            runtime_id=runtime_id,
            server_url=resolved_url,
            hostname=hostname,
            agent_id=agent_id,
            capability_id=capability_id,
            artifact_root=artifact_root,
            idle_sleep_seconds=idle_sleep_seconds,
            max_iterations=max_iterations,
            max_local_recoveries=max_local_recoveries,
            host_kind=host_kind,
            adapter_kind=adapter_kind,
        )
    finally:
        if not had_previous_token:
            os.environ.pop("AGP_RUNTIME_BEARER_TOKEN", None)
        elif previous_token != os.environ.get("AGP_RUNTIME_BEARER_TOKEN"):
            os.environ["AGP_RUNTIME_BEARER_TOKEN"] = previous_token or ""
