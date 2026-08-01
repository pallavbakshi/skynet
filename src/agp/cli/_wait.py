"""Wait, attach, and peek commands."""

from __future__ import annotations

import time as _time
from datetime import UTC

import typer

from agp.cli import app
from agp.cli._helpers import (
    _cli_client,
    _format_http_error,
    _poll_jobs_until_done,
    _poll_until_done,
    _print_job_result,
    _print_peek_tip,
)


@app.command(name="wait")
def wait_cmd(
    job_ids: list[str] = typer.Argument(..., help="One or more job IDs to wait on."),
    server_url: str = typer.Option(None, help="CP URL."),
    timeout: int = typer.Option(300, "--poll-timeout", "--timeout", help="Wait timeout in seconds (default: 300)."),
) -> None:
    """Re-attach to running jobs and wait for their results.

    Accepts one or more job IDs. When multiple IDs are given, polls all of
    them concurrently and prints each result as soon as it is ready — you
    don't have to wait for the slowest job before seeing faster ones.

    The server lets jobs run up to 60 minutes before the CP fails them, but
    the CLI's default ``--poll-timeout`` is 300s. For long tasks (reviews,
    full-codebase scans), pass a larger window up front:

      agp wait job_abc123 --poll-timeout 3600   # block up to the CP limit

    If ``wait`` times out, the job IS STILL RUNNING — do not panic, do not
    resend. Re-run ``agp wait`` with a larger ``--poll-timeout``, or use
    ``agp peek <agent>`` to see live terminal state.

    Examples:

      agp wait job_abc123
      agp wait job_abc123 job_def456 job_ghi789
      agp wait job_abc123 --poll-timeout 3600
    """
    import httpx as _httpx

    had_failure = False

    def _print_timeout_hint(jid: str, agent: str) -> None:
        typer.echo(f"wait timeout — {jid} IS STILL RUNNING (not failed)", err=True)
        typer.echo("The CLI stopped polling. Server lets the job run up to 60 minutes total.", err=True)
        typer.echo("DO NOT resend. Be patient. What to do next:", err=True)
        if agent and agent != "?":
            typer.echo(f"  agp peek {agent}                    # see live terminal", err=True)
        typer.echo(f"  agp wait {jid} --poll-timeout 3600  # block until done", err=True)
        typer.echo(f"  agp result {jid}                    # fetch output once complete", err=True)

    # Dedupe job_ids while preserving order so `agp wait job_a job_a` doesn't
    # double-print or skew the N/M heartbeat denominator.
    job_ids = list(dict.fromkeys(job_ids))

    with _cli_client(server_url) as client:
        # Phase 1: triage — print already-done results, collect pending jobs
        pending: list[str] = []
        pending_agents: dict[str, str] = {}
        for jid in job_ids:
            try:
                job = client.get_job(jid)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                had_failure = True
                continue
            if job["status"] in ("completed", "failed", "cancelled"):
                _print_job_result(job, client)
                if job["status"] == "failed":
                    had_failure = True
            else:
                pending.append(jid)
                pending_agents[jid] = job.get("target_agent_id", "?")

        if not pending:
            if had_failure:
                raise typer.Exit(1)
            return

        # Phase 2a: single pending job — use rich per-job progress
        if len(pending) == 1:
            jid = pending[0]
            agent_id = pending_agents[jid]
            typer.echo(f"[..] Re-attaching to {jid} (agent={agent_id})...")
            _print_peek_tip(agent_id)
            job_age: float = 0.0
            try:
                from datetime import datetime
                src_job = client.get_job(jid)
                created_raw = src_job.get("created_at")
                if created_raw:
                    created = created_raw if isinstance(created_raw, datetime) else datetime.fromisoformat(str(created_raw))
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=UTC)
                    job_age = (datetime.now(UTC) - created).total_seconds()
            except Exception:
                pass
            try:
                job, timed_out = _poll_until_done(client, jid, timeout, job_created_at=job_age)
            except KeyboardInterrupt:
                import time
                typer.echo("", err=True)
                typer.echo("Detached 1 job (still running in background):", err=True)
                typer.echo(f"  agp wait {jid}", err=True)
                typer.echo("\nCtrl+C again within 2s to stop it.", err=True)
                try:
                    time.sleep(2)
                except KeyboardInterrupt:
                    typer.echo(f"\nStopping {jid}...", err=True)
                    try:
                        client.interrupt(jid)
                        typer.echo(f"  {jid} — interrupted", err=True)
                    except Exception:
                        typer.echo(f"  {jid} — could not interrupt", err=True)
                raise typer.Exit(0)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                had_failure = True
            else:
                if not timed_out:
                    _print_job_result(job, client)
                    if job["status"] == "failed":
                        had_failure = True
                else:
                    _print_timeout_hint(jid, agent_id)
                    had_failure = True
            if had_failure:
                raise typer.Exit(1)
            return

        # Phase 2b: multiple pending jobs — concurrent poll, stream completions
        typer.echo(f"[..] Waiting on {len(pending)} job(s):")
        for jid in pending:
            typer.echo(f"     {jid}  agent={pending_agents[jid]}  (agp peek {pending_agents[jid]})")

        # Caller-side pending set stays in sync via on_complete so Ctrl+C
        # knows exactly which jobs are still unfinished. Discard happens
        # BEFORE any network-blocking print — otherwise a Ctrl+C during
        # _print_job_result (which fetches artifacts) would leak a completed
        # job back into pending_set.
        pending_set = set(pending)

        def _on_complete(jid: str, job: dict) -> None:
            nonlocal had_failure
            pending_set.discard(jid)
            _print_job_result(job, client)
            if job["status"] == "failed":
                had_failure = True

        def _on_error(jid: str, exc: Exception) -> None:
            nonlocal had_failure
            pending_set.discard(jid)
            typer.echo(f"error: {jid} — {exc}", err=True)
            typer.echo("  (job may have been deleted or purged)", err=True)
            had_failure = True

        try:
            _, still_pending = _poll_jobs_until_done(
                client, pending, timeout,
                on_complete=_on_complete, on_error=_on_error,
            )
        except KeyboardInterrupt:
            import time
            remaining = sorted(pending_set)
            typer.echo("", err=True)
            typer.echo(f"Detached {len(remaining)} job(s) (still running in background):", err=True)
            for jid in remaining:
                typer.echo(f"  agp wait {jid}", err=True)
            typer.echo("\nCtrl+C again within 2s to stop all jobs.", err=True)
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                typer.echo(f"\nStopping {len(remaining)} job(s)...", err=True)
                for jid in remaining:
                    try:
                        client.interrupt(jid)
                        typer.echo(f"  {jid} — interrupted", err=True)
                    except Exception:
                        typer.echo(f"  {jid} — could not interrupt", err=True)
            raise typer.Exit(0)

        for jid in sorted(still_pending):
            _print_timeout_hint(jid, pending_agents.get(jid, "?"))
            had_failure = True

    if had_failure:
        raise typer.Exit(1)


def _peek_agent_status(agent_id: str, server_url: str | None) -> str | None:
    """Return a short status string for the peek header, or None on failure."""
    try:
        with _cli_client(server_url) as client:
            info = client.get_agent(agent_id)
            status = (info.get("status") or "unknown").upper()
            job_id = info.get("current_job_id") or ""
            if status == "BUSY" and job_id:
                return f"BUSY on {job_id}"
            return status
    except Exception:
        return None


def _try_local_peek(agent_id: str, *, lines: int = 0) -> str | None:
    """Try to capture terminal content locally (fast path)."""
    import shutil
    import subprocess

    # Try tmux first (most common host kind)
    if shutil.which("tmux"):
        session_name = f"agp-{agent_id}"
        try:
            check = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True, timeout=3,
            )
            if check.returncode == 0:
                args = ["tmux", "capture-pane", "-t", session_name, "-p"]
                if lines and lines > 0:
                    args.extend(["-S", str(-lines)])
                result = subprocess.run(args, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    return result.stdout
        except Exception:
            pass

    # Try wezterm
    if shutil.which("wezterm"):
        try:
            result = subprocess.run(
                ["wezterm", "cli", "list", "--format", "json"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                import json as _json
                for pane in _json.loads(result.stdout):
                    title = pane.get("title", "")
                    if f"agp-{agent_id}" in title or title == agent_id:
                        pane_id = pane.get("pane_id")
                        args = ["wezterm", "cli", "get-text", "--pane-id", str(pane_id)]
                        if lines and lines > 0:
                            args.extend(["--start-line", str(-lines)])
                        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            return result.stdout
        except Exception:
            pass

    return None


@app.command()
def attach(
    agent_id: str = typer.Argument(..., help="Agent ID to attach to."),
) -> None:
    """Attach to an agent's live terminal session.

    Opens an interactive view of the agent's tmux or wezterm pane.
    Use Ctrl+B D (tmux) to detach without stopping the agent.

    Examples:

      agp attach claude-dev
      agp attach codex-dev
    """
    import os
    import subprocess

    session_name = f"agp-{agent_id}"

    # Try tmux
    try:
        check = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if check.returncode == 0:
            os.execvp("tmux", ["tmux", "attach", "-t", session_name])
    except FileNotFoundError:
        pass

    # Try wezterm — smallops marks panes with title "SMALLOPS:{agent_id}"
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            import json as _json
            marker = f"SMALLOPS:{agent_id}"
            for pane in _json.loads(result.stdout):
                if pane.get("window_title") == marker or pane.get("tab_title") == marker:
                    pane_id = str(pane.get("pane_id"))
                    subprocess.run(["wezterm", "cli", "activate-pane", "--pane-id", pane_id], check=False)
                    typer.echo(f"Activated wezterm pane {pane_id} for {agent_id}")
                    return
    except FileNotFoundError:
        pass

    typer.echo(f"No local session found for {agent_id} (looked for '{session_name}').", err=True)
    typer.echo(f"Use 'agp peek {agent_id}' for remote agents.", err=True)
    raise typer.Exit(1)


@app.command()
def peek(
    agent_id: str = typer.Argument(..., help="Agent ID to peek at."),
    lines: int = typer.Option(0, "--lines", "-n", help="Scrollback lines to capture (0 = visible screen only)."),
    timeout: float = typer.Option(45.0, "--timeout", help="Max seconds to wait for remote peek result."),
    server_url: str = typer.Option(None, help="CP URL."),
) -> None:
    """Show live terminal content of an agent's runtime.

    Peek is the universal way to inspect what any agent is doing right now.
    It works regardless of terminal host (tmux, wezterm) and regardless of
    whether the agent is local or on a remote server.

    By default captures the visible screen. Use --lines N to include
    scrollback history (useful for seeing earlier output or error traces).

    Local agents:   captured directly from tmux/wezterm (sub-second).
    Remote agents:  captured via the control plane on the next heartbeat (~5-15s).

    Common use cases:

      agp peek claude-dev              # what is it doing right now?
      agp peek claude-dev -n 200       # show last 200 lines of scrollback
      agp peek codex-reviewer          # inspect a remote agent on another server
      agp peek claude-dev --timeout 5  # fail fast if agent is slow to respond

    Use peek when:
      - A job is running long and you want to see progress
      - A job failed and you want to see the agent's terminal state
      - You want to verify an agent is actually working, not stuck
      - You need to debug a remote agent without SSH access
    """
    import httpx as _httpx

    # Fast path: try local capture first
    local_text = _try_local_peek(agent_id, lines=lines)
    if local_text is not None:
        # Show agent status header so users know if output is live or stale
        agent_status = _peek_agent_status(agent_id, server_url)
        if agent_status:
            typer.echo(f"[{agent_id} — {agent_status}]", err=True)
        typer.echo(local_text, nl=False)
        return

    # Remote path: request via CP
    with _cli_client(server_url) as client:
        try:
            req = client.request_peek(agent_id, lines=lines)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)

        request_id = req["request_id"]
        runtime_id = req.get("runtime_id", "?")
        typer.echo(f"[..] Peek requested for {agent_id} (runtime={runtime_id}). Waiting for heartbeat...", err=True)

        start = _time.monotonic()
        while _time.monotonic() - start < timeout:
            elapsed = int(_time.monotonic() - start)
            try:
                result = client.get_peek_result(agent_id, request_id)
            except _httpx.HTTPStatusError as exc:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

            if result.get("status") == "ready":
                typer.echo(result["text"], nl=False)
                return

            typer.echo(f"\r[..] Waiting for heartbeat... ({elapsed}s)", err=True, nl=False)
            _time.sleep(1.0)

        typer.echo("", err=True)  # newline after progress
        typer.echo(
            f"Timed out after {int(timeout)}s waiting for peek result. "
            "The runtime may be offline or slow to heartbeat.",
            err=True,
        )
        raise typer.Exit(1)


