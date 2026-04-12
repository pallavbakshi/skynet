"""Nudge and cleanup commands."""

from __future__ import annotations

from pathlib import Path

import typer

from agp.cli import app
from agp.cli._helpers import _cli_client, _format_http_error


def _format_human_nudge(message: str) -> str:
    return message


@app.command()
def nudge(
    target: str = typer.Argument(..., help="Target agent ID."),
    message: str = typer.Argument(..., help="Message to inject into the agent's terminal."),
) -> None:
    """Inject a message directly into an agent's terminal — instantly.

    Writes the message to a via-file and types the reference string
    into the agent's terminal pane.  No queue, no heartbeat, no wait.
    The agent sees the message on its next input read.

    Works with both tmux and wezterm sessions (local only).

    Examples:

      agp nudge claude-dev "stop and focus on the login bug instead"
      agp nudge orchestrator "the deadline moved to Friday"
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    payload = _format_human_nudge(message)

    # Write nudge to a plain file — NOT write_via_file (which adds
    # BEGIN TASK / END TASK markers and a task-style reference string).
    # Nudges are system reminders, not tasks.  The original so.send()
    # poll must not see nudge markers in the terminal output.
    nudge_dir = Path("/tmp/smallops")
    nudge_dir.mkdir(mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="nudge-", suffix=".md", dir=str(nudge_dir))
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    ref = f"Read the file {tmp} and follow the instructions inside."
    session_name = f"agp-{target}"

    # Try tmux
    try:
        check = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, timeout=3,
        )
        if check.returncode == 0:
            # Match smallops send_text: -l (literal) text, brief pause, then Enter
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l", ref],
                check=True, capture_output=True, timeout=3,
            )
            time.sleep(0.05)
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                check=True, capture_output=True, timeout=3,
            )
            typer.echo(f"nudge sent to {target}")
            return
    except FileNotFoundError:
        pass

    # Try wezterm
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            import json as _json
            marker = f"SMALLOPS:{target}"
            for pane in _json.loads(result.stdout):
                if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                    pane_id = str(pane.get("pane_id"))
                    subprocess.run(
                        ["wezterm", "cli", "send-text", "--pane-id", pane_id, "--no-paste", ref + "\n"],
                        check=True, capture_output=True, timeout=3,
                    )
                    typer.echo(f"nudge sent to {target}")
                    return
    except FileNotFoundError:
        pass

    typer.echo(f"No local session found for {target} (looked for '{session_name}').", err=True)
    raise typer.Exit(1)


@app.command()
def cleanup(
    workspace: str = typer.Argument(None, help="Workspace directory (default: cwd)."),
    keep_temp_artifacts: bool = typer.Option(False, "--keep-temp-artifacts", help="Skip temp artifact cleanup."),
) -> None:
    """Remove AGP temp artifacts and stale result files."""
    from agp.runtime._attachments import cleanup_temp_artifacts, cleanup_stale_result_files

    ws = Path(workspace) if workspace else None
    total = 0
    if not keep_temp_artifacts:
        n = cleanup_temp_artifacts(ws)
        if n:
            typer.echo(f"Cleaned {n} temp artifact director{'ies' if n != 1 else 'y'}.")
        total += n
    n = cleanup_stale_result_files()
    if n:
        typer.echo(f"Cleaned {n} stale result file{'s' if n != 1 else ''}.")
    total += n
    if total == 0:
        typer.echo("Nothing to clean.")


