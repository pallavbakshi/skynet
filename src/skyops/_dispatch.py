"""``skyops send``, ``skyops watch``, ``skyops jobs``, etc. — dispatch commands via AgpClient."""

from __future__ import annotations

import json
import time

import httpx
import typer

from skyops._client import build_client

dispatch_app = typer.Typer(help="Work dispatch and job management.")


def _client():
    return build_client()


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@dispatch_app.command("send")
def send(
    agent_id: str = typer.Argument(help="Target agent ID."),
    task: str = typer.Argument(help="Task text to send."),
    metadata_json: str | None = typer.Option(None, "--metadata", "-m", help="JSON metadata."),
    idempotency_key: str | None = typer.Option(None, "--key", "-k", help="Idempotency key."),
) -> None:
    """Send work to an agent via AgpClient.send()."""
    metadata = json.loads(metadata_json) if metadata_json else None
    with _client() as client:
        result = client.send("agent", agent_id, task, metadata=metadata, idempotency_key=idempotency_key)
    _emit(result)


@dispatch_app.command("watch")
def watch(
    job_id: str = typer.Argument(help="Job ID to watch."),
    poll_interval: float = typer.Option(1.0, "--interval", "-i", help="Poll interval seconds."),
    max_polls: int = typer.Option(120, "--max-polls", help="Maximum poll iterations."),
) -> None:
    """Watch a job until it reaches a terminal state."""
    with _client() as client:
        snapshots = client.watch_job(job_id, poll_interval=poll_interval, max_polls=max_polls)
    if snapshots:
        last = snapshots[-1]["job"]
        _emit(last)


@dispatch_app.command("jobs")
def list_jobs(
    status: str | None = typer.Option(None, "--status", "-s", help="Filter by status."),
    agent: str | None = typer.Option(None, "--agent", "-a", help="Filter by target agent."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results."),
) -> None:
    """List jobs."""
    with _client() as client:
        result = client.list_jobs(status=status, target_agent_id=agent, limit=limit)
    _emit(result)


@dispatch_app.command("agents")
def list_agents(
    status: str | None = typer.Option(None, "--status", "-s", help="Filter by status."),
    capability: str | None = typer.Option(None, "--capability", "-c", help="Filter by capability."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results."),
) -> None:
    """List agents."""
    with _client() as client:
        result = client.list_agents(status=status, capability_id=capability, limit=limit)
    _emit(result)


@dispatch_app.command("capabilities")
def list_capabilities(
    name: str | None = typer.Option(None, "--name", "-n", help="Filter by capability name."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results."),
) -> None:
    """List capabilities."""
    with _client() as client:
        result = client.list_capabilities(name=name, limit=limit)
    _emit(result)


@dispatch_app.command("capability")
def inspect_capability(
    target: str = typer.Argument(help="Capability ID or display name."),
) -> None:
    """Inspect a capability by ID or display name."""
    with _client() as client:
        try:
            result = client.get_capability(target)
        except httpx.HTTPStatusError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise
            items = [
                item
                for item in client.list_capabilities(name=target, limit=100).get("items", [])
                if item.get("name") == target
            ]
            if not items:
                typer.echo(f"Capability not found: {target}", err=True)
                raise typer.Exit(1)
            if len(items) > 1:
                typer.echo(
                    "Capability name is ambiguous; use a capability_id. Matches: "
                    + ", ".join(
                        f"{item.get('capability_id')} ({item.get('name')}:{item.get('version')})"
                        for item in items
                    ),
                    err=True,
                )
                raise typer.Exit(1)
            result = client.get_capability(items[0]["capability_id"])
    _emit(result)


@dispatch_app.command("interrupt")
def interrupt(
    job_id: str = typer.Argument(help="Job ID to interrupt."),
) -> None:
    """Interrupt a running job."""
    with _client() as client:
        result = client.interrupt(job_id)
    _emit(result)


@dispatch_app.command("fetch")
def fetch(
    artifact_id: str = typer.Argument(help="Artifact ID."),
    content: bool = typer.Option(False, "--content", help="Include artifact content."),
) -> None:
    """Fetch an artifact."""
    with _client() as client:
        result = client.fetch_artifact(artifact_id, content=content)
    _emit(result)


@dispatch_app.command("handoff")
def handoff(
    job_id: str = typer.Argument(help="Source job ID."),
    target_type: str = typer.Option("agent", "--type", "-t", help="Target type: agent or capability."),
    target_id: str = typer.Option(..., "--target", help="Target agent or capability ID."),
    task: str = typer.Option(..., "--task", help="Task text for child job(s)."),
    artifact_ids: str | None = typer.Option(None, "--artifacts", help="Comma-separated artifact IDs to pass through."),
) -> None:
    """Create a handoff from a source job to a child job."""
    targets = [{"type": target_type, "id": target_id}]
    message = {"text": task, "metadata": {}}
    art_ids = [a.strip() for a in artifact_ids.split(",")] if artifact_ids else []
    with _client() as client:
        result = client.handoff(job_id, targets, message, artifact_ids=art_ids)
    _emit(result)


@dispatch_app.command("nudge")
def nudge(
    agent_id: str = typer.Argument(help="Target agent ID to nudge."),
    message: str = typer.Argument(help="Nudge message text."),
    priority: int = typer.Option(1, "--priority", "-p", help="Priority 1-4 (1=highest)."),
    job_id: str | None = typer.Option(None, "--job", help="Associated job ID."),
) -> None:
    """Send a human co-pilot nudge to an agent."""
    with _client() as client:
        result = client.create_nudge(agent_id, message, priority=priority, source="human", job_id=job_id)
    _emit(result)


@dispatch_app.command("undrain")
def undrain(
    agent_id: str = typer.Argument(help="Agent ID to undrain."),
) -> None:
    """Lift draining status and return agent to IDLE."""
    with _client() as client:
        result = client.agent_undrain(agent_id)
    _emit(result)


@dispatch_app.command("deliveries")
def list_deliveries(
    state: str | None = typer.Option(None, "--state", help="Filter by delivery state."),
    job_id: str | None = typer.Option(None, "--job", help="Filter by job ID."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results."),
) -> None:
    """List queue deliveries."""
    with _client() as client:
        result = client.list_deliveries(state=state, job_id=job_id, limit=limit)
    _emit(result)
