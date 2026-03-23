"""``skyops agent`` — operator terminal controls for agents."""

from __future__ import annotations

import typer

agent_debug_app = typer.Typer(help="Agent terminal debugging commands.")


@agent_debug_app.command("nudge-loop")
def agent_nudge_loop(
    target: str = typer.Argument(..., help="Orchestrator agent ID to deliver nudges to."),
    session: str | None = typer.Option(None, "--session", help="Tmux session name (default: agp-<target>)."),
    server_url: str | None = typer.Option(None, "--server-url", help="CP URL."),
    poll_seconds: float = typer.Option(2.0, "--poll-seconds", help="Poll interval for new nudges."),
    idle_polls: int = typer.Option(3, "--idle-polls", help="Consecutive stable polls before injecting."),
    max_iterations: int | None = typer.Option(None, "--max-iterations", help="Stop after N deliveries."),
) -> None:
    """Deliver queued nudges into an orchestrator tmux session."""
    from agp.cli import nudge_loop as agp_nudge_loop

    agp_nudge_loop(
        target=target,
        session=session,
        server_url=server_url,
        poll_seconds=poll_seconds,
        idle_polls=idle_polls,
        max_iterations=max_iterations,
    )
