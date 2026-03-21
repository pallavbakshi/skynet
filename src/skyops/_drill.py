"""``skyops drill`` — failure injection drill commands."""

from __future__ import annotations

import json

import typer

drill_app = typer.Typer(help="Failure injection drills.")

_SCENARIOS = [
    "lease_expiry_requeue",
    "duplicate_terminal_replay",
    "artifact_store_write_failure",
    "queue_redelivery_after_consumer_restart",
    "repeated_fencing_stale_owner",
    "control_plane_restart_active_work",
]


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


@drill_app.command("list")
def drill_list() -> None:
    """List available failure injection scenarios."""
    for s in _SCENARIOS:
        typer.echo(f"  {s}")


@drill_app.command("run")
def drill_run(
    scenario: str = typer.Argument(help="Scenario name."),
) -> None:
    """Run a named failure injection drill."""
    from agp._ops_helpers import run_failure_injection_scenario

    result = run_failure_injection_scenario(scenario=scenario)
    _emit(result)


@drill_app.command("full")
def drill_full() -> None:
    """Run all failure injection drills sequentially."""
    from agp._ops_helpers import run_failure_injection_scenario

    for scenario in _SCENARIOS:
        typer.echo(f"\n{'─' * 40}")
        typer.echo(f"Running: {scenario}")
        typer.echo(f"{'─' * 40}")
        try:
            result = run_failure_injection_scenario(scenario=scenario)
            _emit(result)
        except Exception as e:
            typer.echo(f"FAILED: {e}", err=True)
    typer.echo("\nAll drills complete.")
