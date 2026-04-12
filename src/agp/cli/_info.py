"""Info command — agent, runtime, capability deep-dive."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import typer

from agp.cli import app
from agp.cli._helpers import (
    _cli_client, _format_duration, _format_http_error,
    _heartbeat_age_seconds, _SEPARATOR,
)


def _print_capability_blueprint(cap: dict, *, indent: str = "") -> None:
    """Print capability blueprint fields."""
    typer.echo(f"{indent}MODEL:        {cap.get('model_ref') or '-'}")
    tier = cap.get("resource_tier") or "-"
    typer.echo(f"{indent}TIER:         {tier}")
    typer.echo(f"{indent}PERMISSION:   {cap.get('permission_profile') or 'default'}")

    reqs = cap.get("runtime_requirements_json") or {}

    network = reqs.get("network")
    filesystem = reqs.get("filesystem")
    if network or filesystem:
        typer.echo(f"{indent}ACCESS:")
        if network:
            typer.echo(f"{indent}  Network:    {network}")
        if filesystem:
            typer.echo(f"{indent}  Filesystem: {filesystem}")

    tools = reqs.get("tools")
    if tools and isinstance(tools, list):
        typer.echo(f"{indent}PRE-INSTALLED TOOLS:")
        for t in tools:
            typer.echo(f"{indent}  - {t}")

    restrictions = reqs.get("restrictions")
    if restrictions and isinstance(restrictions, list):
        typer.echo(f"{indent}RESTRICTIONS:")
        for r in restrictions:
            typer.echo(f"{indent}  - {r}")


@app.command()
def info(
    target: str = typer.Argument(..., help="Agent ID, runtime ID, or capability name/ID."),
    server_url: str = typer.Option(None, help="CP URL."),
    diagnose: bool = typer.Option(False, "--diagnose", "-d", help="Include diagnostic details (runtime logs, registration)."),
    output_json: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Deep-dive context for an agent, runtime, or capability.

    Accepts an agent ID (e.g. agt_local), runtime ID (e.g. rtm_abc),
    or capability ID/name (e.g. cap_python).

    Use --diagnose to include runtime logs and registration details.
    """
    from datetime import datetime, timezone
    import httpx as _httpx

    with _cli_client(server_url) as client:
        # Runtime detection: if target starts with "rtm_" or "rtm-", try runtime first
        if target.startswith("rtm_") or target.startswith("rtm-"):
            _info_runtime(target, client, output_json=output_json, diagnose=diagnose)
            return

        # Try agent first, fall back to capability
        agent = None
        try:
            agent = client.get_agent(target)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                typer.echo(_format_http_error(exc), err=True)
                raise typer.Exit(1)

        if agent is not None:
            if output_json:
                _info_agent_json(agent, client, diagnose=diagnose)
            else:
                _info_agent(agent, client)
                if diagnose:
                    _info_agent_diagnose(agent, client)
        else:
            _info_capability(target, client, output_json=output_json)


def _info_agent(agent: dict, client) -> None:
    from datetime import datetime, timezone

    agent_id = agent["agent_id"]

    typer.echo(_SEPARATOR)
    typer.echo(f"      AGENT INFO: {agent_id}")
    typer.echo(_SEPARATOR)

    typer.echo(f"STATUS:       {agent.get('status', '?').upper()}")
    typer.echo(f"CAPABILITIES: {', '.join(agent.get('capabilities', [])) or '-'}")

    # Heartbeat
    now = datetime.now(timezone.utc)
    hb_age = _heartbeat_age_seconds(agent.get("last_heartbeat_at"))
    if hb_age is not None:
        typer.echo(f"HEARTBEAT:    {hb_age:.0f}s ago")

    # Queue depth
    qdepth = int(agent.get("queue_depth", 0) or 0)
    typer.echo(f"QUEUE_DEPTH:  {qdepth}")

    # Current job for busy agents
    if agent.get("status") == "busy":
        try:
            running = client.list_jobs(status="running", target_agent_id=agent_id, limit=1)
            items = running.get("items", [])
            if items:
                job = items[0]
                try:
                    created = datetime.fromisoformat(job["created_at"])
                    elapsed = (now - created).total_seconds()
                    duration = _format_duration(elapsed)
                except Exception:
                    duration = "?"
                typer.echo(f"CURRENT_JOB:  {job['job_id']} (Running for {duration})")
        except Exception:
            pass

    # Uptime
    created_at = agent.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(created_at)
            uptime = (now - created).total_seconds()
            typer.echo(f"UPTIME:       {_format_duration(uptime)}")
        except Exception:
            pass

    workspace = agent.get("workspace_ref") or "-"
    typer.echo(f"WORKSPACE:    {workspace}")

    # Runtime binding — query by agent_id instead of guessing runtime ID prefixes
    try:
        rt_page = client.ops_list_runtimes(limit=200)
        bound_rts = [
            rt for rt in rt_page.get("items", [])
            if rt.get("agent_id") == agent_id
        ]
        if bound_rts:
            rt = bound_rts[0]
            typer.echo(f"RUNTIME:      {rt.get('runtime_id', '?')} ({rt.get('hostname', '?')})")
        else:
            typer.echo("RUNTIME:      (unbound)")
    except Exception:
        typer.echo("RUNTIME:      (unbound)")

    # Recent jobs
    try:
        jobs_data = client.list_jobs(target_agent_id=agent_id, limit=5)
        recent = jobs_data.get("items", [])
        if recent:
            typer.echo(f"\nRECENT JOBS ({len(recent)}):")
            for j in recent:
                typer.echo(f"  {j.get('job_id', '?')}  {j.get('status', '?')}")
    except Exception:
        pass


def _info_capability(target: str, client, *, output_json: bool = False) -> None:
    import httpx as _httpx

    # Try by ID first, then search by name
    cap = None
    try:
        cap = client.get_capability(target)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
    except _httpx.RequestError as exc:
        typer.echo(f"unreachable: {exc}", err=True)
        raise typer.Exit(1)

    if cap is None:
        try:
            results = client.list_capabilities(name=target)
            items = results.get("items", [])
            if items:
                cap = items[0]
        except _httpx.HTTPStatusError as exc:
            typer.echo(_format_http_error(exc), err=True)
            raise typer.Exit(1)
        except _httpx.RequestError as exc:
            typer.echo(f"unreachable: {exc}", err=True)
            raise typer.Exit(1)

    if cap is None:
        typer.echo(f"Not found: {target} (not an agent ID or capability name)", err=True)
        raise typer.Exit(1)

    if output_json:
        typer.echo(json.dumps(cap, indent=2, default=str))
        return

    cap_name = cap.get("name", cap.get("capability_id", target))
    typer.echo(_SEPARATOR)
    typer.echo(f"      CAPABILITY INFO: {cap_name}")
    typer.echo(_SEPARATOR)
    _print_capability_blueprint(cap)


def _info_agent_diagnose(agent: dict, client) -> None:
    """Print diagnostic details for an agent (runtime binding, logs, registration)."""
    agent_id = agent["agent_id"]

    # Runtime binding details
    rt = None
    try:
        rt_page = client.ops_list_runtimes(limit=200)
        bound_rts = [
            r for r in rt_page.get("items", [])
            if r.get("agent_id") == agent_id
        ]
        if bound_rts:
            rt = bound_rts[0]
    except Exception:
        pass

    typer.echo(f"\n--- DIAGNOSTICS ---")
    typer.echo(f"REGISTERED:   {agent.get('created_at', '?')}")

    if rt:
        typer.echo(f"\nRuntime Binding:")
        typer.echo(f"  runtime_id: {rt.get('runtime_id', '?')}")
        typer.echo(f"  status:     {rt.get('status', '?')}")
        typer.echo(f"  host:       {rt.get('hostname', '?')}")

        # Recent runtime logs
        runtime_id = rt.get("runtime_id")
        if runtime_id:
            try:
                logs = client.logs_runtime(runtime_id, limit=20)
                entries = logs.get("entries", logs) if isinstance(logs, dict) else logs
                if isinstance(entries, list) and entries:
                    entries = entries[-10:]
                    typer.echo(f"\n  Recent Logs (last {len(entries)}):")
                    for entry in entries:
                        if isinstance(entry, dict):
                            ts = entry.get("created_at", "?")
                            action = entry.get("action", entry.get("kind", "?"))
                            typer.echo(f"    [{ts}] {action}")
                        else:
                            typer.echo(f"    {str(entry)[:120]}")
            except Exception as logs_exc:
                typer.echo(f"  [warn] Failed to fetch runtime logs: {logs_exc}", err=True)
    else:
        typer.echo("Runtime Binding: none")

    # Extended job history
    try:
        jobs_data = client.list_jobs(target_agent_id=agent_id, limit=10)
        jobs = jobs_data.get("items", [])
        if jobs:
            typer.echo(f"\nJob History ({len(jobs)}):")
            for j in jobs:
                typer.echo(f"  {j.get('job_id', '?')}  status={j.get('status', '?')}  created={j.get('created_at', '?')}")
        else:
            typer.echo(f"\nJob History: none")
    except Exception:
        pass


def _info_agent_json(agent: dict, client, *, diagnose: bool = False) -> None:
    """Output agent info as JSON."""
    agent_id = agent["agent_id"]
    payload: dict = {"agent": agent}

    if diagnose:
        # Runtime binding
        try:
            rt_page = client.ops_list_runtimes(limit=200)
            bound_rts = [
                r for r in rt_page.get("items", [])
                if r.get("agent_id") == agent_id
            ]
            payload["runtime"] = bound_rts[0] if bound_rts else None
        except Exception:
            payload["runtime"] = None

        # Recent jobs
        try:
            jobs_data = client.list_jobs(target_agent_id=agent_id, limit=10)
            payload["recent_jobs"] = jobs_data.get("items", [])
        except Exception:
            payload["recent_jobs"] = []

    typer.echo(json.dumps(payload, indent=2, default=str))


def _info_runtime(runtime_id: str, client, *, output_json: bool = False, diagnose: bool = False) -> None:
    """Show runtime info — reuses diagnose runtime rendering."""
    import httpx as _httpx
    try:
        rt = client.ops_get_runtime(runtime_id)
    except _httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            typer.echo(f"Runtime '{runtime_id}' not found.", err=True)
            raise typer.Exit(1)
        raise

    payload: dict = {
        "runtime": rt,
        "agents": rt.get("agents", []),
    }

    if diagnose:
        payload["recent_logs"] = []
        try:
            logs = client.logs_runtime(runtime_id, limit=20)
            payload["recent_logs"] = logs.get("entries", logs) if isinstance(logs, dict) else logs
        except Exception as logs_exc:
            typer.echo(f"[warn] Failed to fetch runtime logs: {logs_exc}", err=True)

    if output_json:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    typer.echo(f"Runtime: {runtime_id}")
    typer.echo(f"  status:     {rt.get('status', '?')}")
    hb = rt.get("heartbeat_age_seconds")
    if hb is None:
        hb = _heartbeat_age_seconds(rt.get("last_heartbeat_at"))
    typer.echo(f"  heartbeat:  {f'{hb:.0f}s ago' if hb is not None else 'never'}")
    typer.echo(f"  host:       {rt.get('hostname', '?')}")
    typer.echo(f"  registered: {rt.get('created_at', '?')}")

    if payload["agents"]:
        typer.echo(f"\n  Bound Agents:")
        for a in payload["agents"]:
            caps = ", ".join(a.get("capabilities", []))
            typer.echo(f"    {a['agent_id']}  status={a['status']}  caps=[{caps}]")
    else:
        typer.echo(f"\n  Bound Agents: none")

    if diagnose:
        logs = payload.get("recent_logs", [])
        if logs:
            entries = logs[-10:] if isinstance(logs, list) else []
            if entries:
                typer.echo(f"\n  Recent Logs (last {len(entries)}):")
                for entry in entries:
                    if isinstance(entry, dict):
                        ts = entry.get("created_at", "?")
                        action = entry.get("action", entry.get("kind", "?"))
                        typer.echo(f"    [{ts}] {action}")
                    else:
                        typer.echo(f"    {str(entry)[:120]}")


