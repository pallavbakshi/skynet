"""``skyops runtime`` — runtime debugging commands."""

from __future__ import annotations

import json
import socket

import typer

from skyops.config import load_config

runtime_debug_app = typer.Typer(help="Runtime debugging commands.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@runtime_debug_app.command("register")
def runtime_register(
    runtime_id: str = typer.Argument(help="Runtime ID to register."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname (defaults to system hostname)."),
) -> None:
    """Register a runtime with the control plane (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity

    cfg = load_config()
    actual_hostname = hostname or socket.gethostname()
    identity = RuntimeIdentity(
        runtime_id=runtime_id,
        hostname=actual_hostname,
        server_url=f"http://127.0.0.1:{cfg.server.port}",
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
    identity = RuntimeIdentity(
        runtime_id=runtime_id,
        hostname=actual_hostname,
        server_url=f"http://127.0.0.1:{cfg.server.port}",
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
    server_url: str = typer.Option("http://127.0.0.1:7860", "--server-url", help="Server URL."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname."),
    artifact_root: str = typer.Option(".agp-artifacts", "--artifact-root", help="Artifact root."),
    host_kind: str = typer.Option("inprocess", "--host-kind", help="Terminal host kind."),
    adapter_kind: str = typer.Option("default", "--adapter-kind", help="Agent adapter kind."),
) -> None:
    """Run a single iteration of the runtime work loop (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity
    from agp.plugins import build_terminal_host, build_agent_adapter
    from agp.runtime import RuntimeSupervisor

    actual_hostname = hostname or socket.gethostname()
    client = RuntimeClient(
        RuntimeIdentity(
            runtime_id=runtime_id,
            hostname=actual_hostname,
            server_url=server_url,
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
