"""Status, jobs, and result commands."""

from __future__ import annotations

import json

import typer

from agp.cli import app
from agp.cli._helpers import (
    _cli_client, _format_duration, _format_http_error,
    _heartbeat_age_seconds, _make_client, _print_job_result,
    _print_peek_tip, _status_show_heartbeat, _SEPARATOR,
)


@app.command()
def status(
    target: str = typer.Argument(None, help="Job ID or agent ID (optional)."),
    server_url: str = typer.Option(None, help="CP URL."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Minimal output — just agent lines."),
) -> None:
    """System dashboard, or job/agent status.

    With no arguments: combined health + agent overview (replaces ``health`` and ``ls``).
    With a job ID: shows full job details + artifacts.
    With an agent ID: shows agent status, heartbeat, and current job.
    """
    if target is None:
        _status_dashboard(server_url, output_json=output_json, quiet=quiet)
        return
    # Try as job first; on 404, try as agent before giving up
    import httpx as _httpx
    with _cli_client(server_url) as client:
        try:
            job = client.get_job(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)
            # Not a job — try as agent
            try:
                agent = client.get_agent(target)
            except _httpx.HTTPStatusError as exc2:
                if exc2.response.status_code == 404:
                    typer.echo(f"Not found: '{target}' is neither a job ID nor an agent ID.", err=True)
                else:
                    typer.echo(_format_http_error(exc2), err=True)
                raise typer.Exit(1)
            _status_agent(agent, client)
            return
        _status_job_from_data(job, client)


def _status_agent(agent: dict, client) -> None:
    """Show agent status summary."""
    aid = agent["agent_id"]
    typer.echo(f"AGENT:        {aid}")
    typer.echo(f"STATUS:       {agent.get('status', '?').upper()}")
    typer.echo(f"CAPABILITIES: {', '.join(agent.get('capabilities', [])) or '-'}")

    # Heartbeat
    hb_age = _heartbeat_age_seconds(agent.get("last_heartbeat_at"))
    if hb_age is not None:
        typer.echo(f"HEARTBEAT:    {hb_age:.0f}s ago")

    qdepth = int(agent.get("queue_depth", 0) or 0)
    if qdepth:
        typer.echo(f"QUEUE_DEPTH:  {qdepth}")

    workspace = agent.get("workspace_ref")
    if workspace:
        typer.echo(f"WORKSPACE:    {workspace}")

    # Show current job if busy
    if agent.get("status") == "busy":
        try:
            running = client.list_jobs(status="running", target_agent_id=aid, limit=1)
            items = running.get("items", [])
            if items:
                typer.echo(f"CURRENT_JOB:  {items[0]['job_id']}")
                _print_peek_tip(aid)
        except Exception:
            pass


def _status_dashboard(server_url: str | None, *, output_json: bool = False, quiet: bool = False) -> None:
    """Combined system dashboard — health, runtimes, agents, queue."""
    import httpx as _httpx
    from datetime import datetime, timezone

    from agp.cli._helpers import _make_client as _mk
    with _mk(server_url) as client:
        try:
            cp_health = client.health()
        except (_httpx.RequestError, _httpx.HTTPStatusError) as exc:
            typer.echo(f"Control plane unreachable: {exc}", err=True)
            raise typer.Exit(1)

        ops: dict | None = None
        agents: list[dict] = []
        runtimes: list[dict] = []
        try:
            ops = client.ops_health()
        except (_httpx.HTTPStatusError, _httpx.RequestError, RuntimeError):
            pass
        try:
            page = client.list_agents(limit=200)
            agents = page.get("items", [])
        except Exception:
            pass
        try:
            rt_page = client.ops_list_runtimes(limit=200)
            runtimes = rt_page.get("items", [])
        except Exception:
            pass

        # Filter synthetic rtm_ runtimes (created by agent_up, no backing process)
        runtimes = [rt for rt in runtimes if not rt.get("runtime_id", "").startswith("rtm_")]

        # For busy agents, fetch their running job
        agent_jobs: dict[str, dict] = {}
        busy_agents = [a for a in agents if a.get("status") == "busy"]
        if busy_agents:
            try:
                running_jobs = client.list_jobs(status="running", limit=100)
                for j in running_jobs.get("items", []):
                    tid = j.get("target_agent_id")
                    if tid:
                        agent_jobs[tid] = j
            except Exception:
                pass

        if output_json:
            typer.echo(json.dumps({
                "control_plane": cp_health,
                "ops": ops,
                "agents": agents,
                "runtimes": runtimes,
            }, indent=2, default=str))
            return

        if quiet:
            # Minimal output — just agent lines (matches ls -q)
            if not agents:
                typer.echo("(none)")
                return
            now = datetime.now(timezone.utc)
            for agent in agents:
                aid = agent.get("agent_id", "?")
                agent_status = agent.get("status", "?").upper()
                role = ", ".join(agent.get("capabilities", [])) or "-"
                qdepth = int(agent.get("queue_depth", 0) or 0)
                job = agent_jobs.get(aid)
                parts = [f"{aid:<20s}", agent_status]
                if role != "-":
                    parts.append(f"caps=[{role}]")
                if job:
                    parts.append(f"job={job['job_id']}")
                    try:
                        created = datetime.fromisoformat(job["created_at"])
                        elapsed = (now - created).total_seconds()
                        parts.append(f"({_format_duration(elapsed)})")
                    except Exception:
                        pass
                if qdepth > 0:
                    parts.append(f"queue={qdepth}")
                typer.echo("  ".join(parts))
            return

        # ── Control plane health
        cp_data = cp_health.get("data", cp_health)
        cp_status = cp_data.get("status", "unknown")
        typer.echo(f"Control Plane: {cp_status}")
        for k, v in cp_data.get("components", {}).items():
            typer.echo(f"  {k}: {v}")

        # ── Runtimes
        typer.echo(f"\nRuntimes: {len(runtimes)}")
        for rt in runtimes:
            rid = rt.get("runtime_id", "?")
            hb_age = rt.get("heartbeat_age_seconds")
            if hb_age is None:
                hb_age = _heartbeat_age_seconds(rt.get("last_heartbeat_at"))
            hb_str = f"{hb_age:.0f}s ago" if hb_age is not None else "never"
            bound_aid = rt.get("agent_id")
            if bound_aid:
                agents_bound = bound_aid
            else:
                agents_bound = ", ".join(
                    sorted({w.get("agent_id", "?") for w in rt.get("claimed_work", [])})
                ) or "none"
            typer.echo(f"  {rid}  heartbeat={hb_str}  agents=[{agents_bound}]")

        # ── Agents
        now = datetime.now(timezone.utc)
        typer.echo(f"\nAgents: {len(agents)}")
        for agent in agents:
            aid = agent.get("agent_id", "?")
            state = agent.get("status", "unknown")
            caps = ", ".join(agent.get("capabilities", []))
            qdepth = int(agent.get("queue_depth", 0) or 0)
            job = agent_jobs.get(aid)
            parts = [f"  {aid}  status={state}"]
            if caps:
                parts.append(f"caps=[{caps}]")
            if job:
                job_id = job["job_id"]
                try:
                    created = datetime.fromisoformat(job["created_at"])
                    elapsed = (now - created).total_seconds()
                    parts.append(f"job={job_id} ({_format_duration(elapsed)})")
                except Exception:
                    parts.append(f"job={job_id}")
            if qdepth > 0:
                parts.append(f"queue={qdepth}")
            typer.echo("  ".join(parts))

        # ── Queue summary
        if ops:
            queue = ops.get("queue") or {}
            depth = int(queue.get("depth") or 0)
            if depth > 0:
                typer.echo(f"\nQueue depth: {depth}")

        # ── Warnings for agents with queued work but no runtime
        agent_runtime: dict[str, str] = {}
        runtime_health: dict[str, tuple[str, str]] = {}
        for rt in runtimes:
            rid = rt.get("runtime_id", "")
            runtime_health[rid] = (
                str(rt.get("status") or "-").lower(),
                str(rt.get("health_status") or "-").lower(),
            )
            aid = rt.get("agent_id")
            if aid:
                agent_runtime[aid] = rid
        warning_items: list[str] = []
        for agent in agents:
            aid = agent.get("agent_id", "?")
            qdepth = int(agent.get("queue_depth", 0) or 0)
            if qdepth <= 0:
                continue
            bound_rt = agent_runtime.get(aid)
            if not bound_rt:
                warning_items.append(
                    f"- {aid}: {qdepth} queued, no runtime bound. Start or re-register its runtime."
                )
            elif bound_rt in runtime_health:
                rs, hs = runtime_health[bound_rt]
                if rs in {"degraded", "offline"} or hs in {"degraded", "unreachable"}:
                    warning_items.append(
                        f"- {aid}: {qdepth} queued, runtime {bound_rt} heartbeat stale ({hs if hs != '-' else rs}). Restart that runtime."
                    )
        if warning_items:
            typer.echo("\n[WARNINGS]")
            for item in warning_items:
                typer.echo(item)


def _status_job(job_id: str, server_url: str | None) -> None:
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            job = client.get_job(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        _status_job_from_data(job, client)


def _status_job_from_data(job: dict, client) -> None:
    retry_count = job.get("retry_count", 0)
    max_retries = job.get("max_retries", 3)
    job_id = job["job_id"]

    typer.echo(f"JOB_ID:       {job_id}")
    typer.echo(f"AGENT:        {job.get('target_agent_id', 'unknown')}")
    typer.echo(f"STATUS:       {job['status'].upper()}")
    if retry_count > 0:
        typer.echo(f"RETRIES:      {retry_count}/{max_retries}")
    if job.get("latest_run_id"):
        typer.echo(f"RUN:          {job['latest_run_id']}")
    typer.echo(f"CREATED:      {job.get('created_at', '?')}")
    typer.echo(f"UPDATED:      {job.get('updated_at', '?')}")

    if job["status"] in ("queued", "accepted", "leased", "running"):
        _status_show_heartbeat(job_id, client)
        _print_peek_tip(job.get("target_agent_id", ""))

    # Show result/failure artifact if terminal
    if job["status"] in ("completed", "failed") and job.get("result_artifact_id"):
        try:
            art = client.fetch_artifact(job["result_artifact_id"], content=True)
            typer.echo("---")
            typer.echo(art.get("content", "(no content)"))
        except Exception:
            pass
    elif job["status"] == "failed":
        try:
            artifacts = client.list_job_artifacts(job_id, role="failure_evidence")
            items = artifacts.get("items", [])
            if items:
                art = client.fetch_artifact(items[0]["artifact_id"], content=True)
                typer.echo("---")
                typer.echo(art.get("content", "(no content)"))
        except Exception:
            pass
        agent_id = job.get("target_agent_id", "")
        if agent_id:
            typer.echo(f"Tip: inspect the agent's terminal:  agp peek {agent_id}")


@app.command()
def jobs(
    server_url: str = typer.Option(None, help="CP URL."),
    limit: int = typer.Option(10, help="Max jobs to show."),
    agent: str = typer.Option(None, "--agent", help="Filter by agent ID."),
    filter_status: str = typer.Option(None, "--status", help="Filter by status (queued, running, completed, failed)."),
) -> None:
    """List recent jobs."""
    from datetime import datetime, timezone
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            data = client.list_jobs(
                limit=limit,
                target_agent_id=agent,
                status=filter_status,
            )
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        items = data.get("items", [])
        if not items:
            typer.echo("(no jobs)")
            return
        # For failed jobs, fetch the failure reason from events so
        # operators can distinguish infra failures from agent errors.
        failure_reasons: dict[str, str] = {}
        for j in items:
            if j["status"] != "failed":
                continue
            try:
                events_data = client.get_job_events(j["job_id"], limit=50)
                for ev in reversed(events_data.get("items", [])):
                    body = ev.get("body") or {}
                    if ev.get("event_type") == "run.failed":
                        summary = body.get("summary") or {}
                        exc_type = summary.get("exception_type", "")
                        if exc_type:
                            failure_reasons[j["job_id"]] = exc_type
                        break
            except Exception:  # noqa: BLE001
                pass
        now = datetime.now(timezone.utc)
        for j in items:
            retry = f" retry={j['retry_count']}/{j['max_retries']}" if j.get("retry_count", 0) > 0 else ""
            status_str = j["status"]
            reason = failure_reasons.get(j["job_id"])
            if reason:
                status_str = f"failed:{reason}"
            time_info = ""
            try:
                created_raw = j["created_at"]
                created = (
                    created_raw if isinstance(created_raw, datetime)
                    else datetime.fromisoformat(str(created_raw))
                )
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                age_str = _format_duration((now - created).total_seconds()) + " ago"
                elapsed_str = ""
                updated_raw = j.get("updated_at")
                if updated_raw and j["status"] in ("completed", "failed"):
                    updated = (
                        updated_raw if isinstance(updated_raw, datetime)
                        else datetime.fromisoformat(str(updated_raw))
                    )
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    elapsed_str = _format_duration((updated - created).total_seconds())
                time_info = age_str
                if elapsed_str:
                    time_info += f"  took {elapsed_str}"
            except Exception:
                pass
            typer.echo(
                f"  {j['job_id']}  {status_str:<20s}  agent={(j.get('target_agent_id') or '?'):<14s}  {time_info}{retry}"
            )


@app.command()
def result(
    job_id: str = typer.Argument(..., help="Job ID to fetch output for."),
    server_url: str = typer.Option(None, help="CP URL."),
    role: str = typer.Option(None, "--role", help="Artifact role to fetch (default: transcript_log > result > exec_log, or result-first for jobs with output contracts)."),
) -> None:
    """Dump the clean output of a completed job.

    Fetches the transcript (or result artifact) and prints it to stdout
    with no envelope or plumbing.  Useful for piping agent output into
    other tools.

    Jobs with output contracts (e.g. --review) prefer the result artifact
    over transcript, since the result contains the structured output.
    """
    import httpx as _httpx

    with _cli_client(server_url) as client:
        try:
            arts = client.list_job_artifacts(job_id)
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        items = arts.get("items", [])

        # Check job metadata to adjust artifact preference.
        job_data = None
        has_output_contract = False
        job_failed = False
        if not role:
            try:
                job_data = client.get_job(job_id)
                has_output_contract = bool(job_data.get("output_contract_json"))
                job_failed = job_data.get("status") == "failed"
            except Exception:
                pass

        if role:
            candidates = [a for a in items if a.get("role") == role]
        elif job_failed:
            # Failed job: show the failure reason first, fall back to transcript.
            # This applies to both contract and non-contract jobs.
            candidates = (
                [a for a in items if a.get("role") == "failure_evidence"]
                or [a for a in items if a.get("role") == "transcript_log"]
                or [a for a in items if a.get("role") == "result"]
                or [a for a in items if a.get("role") == "exec_log"]
            )
        elif has_output_contract:
            # Successful contract job: result first (structured), then transcript
            candidates = (
                [a for a in items if a.get("role") == "result"]
                or [a for a in items if a.get("role") == "transcript_log"]
                or [a for a in items if a.get("role") == "exec_log"]
            )
        else:
            # Default: result (clean extracted answer) > transcript_log > exec_log
            candidates = (
                [a for a in items if a.get("role") == "result"]
                or [a for a in items if a.get("role") == "transcript_log"]
                or [a for a in items if a.get("role") == "exec_log"]
            )
        if not candidates:
            job_status = str((job_data or {}).get("status") or "").strip().lower()
            if job_status in {"cancelled", "interrupt_requested"}:
                typer.echo("Job was cancelled/interrupted before a result was captured.", err=True)
            typer.echo(f"No output artifact found for job {job_id}", err=True)
            available = [a.get("role") for a in items]
            if available:
                typer.echo(f"Available roles: {', '.join(available)}", err=True)
            raise typer.Exit(1)
        art = candidates[-1]  # latest
        if job_failed and not role:
            art_role = art.get("role", "artifact")
            if has_output_contract:
                typer.echo(f"WARNING: Job failed (output contract violation). Showing {art_role}.", err=True)
            else:
                typer.echo(f"WARNING: Job failed. Showing {art_role}.", err=True)
            typer.echo("  Tip: use --role to select a specific artifact (e.g. --role transcript_log).", err=True)
            target_agent = (job_data or {}).get("target_agent_id", "")
            if target_agent:
                typer.echo(f"  Tip: inspect the agent's terminal:  agp peek {target_agent}", err=True)
            typer.echo("---", err=True)
        try:
            data = client.fetch_artifact(art["artifact_id"], content=True)
            typer.echo(data.get("content") or "(no content)")
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)


