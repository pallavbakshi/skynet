"""Standalone plugin CLI surfaces for terminal hosts, agent adapters, and integrated runners.

These commands will eventually move to the skyops operator CLI.
For now they are registered into the main ``agp`` app as sub-commands.
"""

import json
from pathlib import Path

import typer

from agp.config import settings
from agp.plugins import build_terminal_host, build_agent_adapter
from agp.runtime import (
    StandalonePluginRunner,
    TerminalSession,
)

host_app = typer.Typer(help="Standalone terminal host debugging commands")
adapter_app = typer.Typer(help="Standalone agent adapter debugging commands")
plugin_app = typer.Typer(help="Standalone integrated plugin runner commands")


def _emit(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _host_kwargs(kind: str, workspace: str | None = None, runner: object | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if kind == "wezterm" and workspace:
        kwargs["workspace"] = workspace
    if runner is not None:
        kwargs["runner"] = runner
    return kwargs


def _session(*, session_id: str, agent_id: str, workspace_ref: str | None = None) -> TerminalSession:
    return TerminalSession(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)


def _read_task(*, task: str | None, task_file: str | None) -> str:
    if task and task_file:
        raise typer.BadParameter("provide either task or task-file, not both")
    if task_file:
        return Path(task_file).read_text(encoding="utf-8")
    if task is not None:
        return task
    raise typer.BadParameter("one of task or task-file is required")


@host_app.command("list-hosts")
def host_list_hosts() -> None:
    """List supported terminal host kinds."""
    _emit({"items": ["inprocess", "wezterm", "tmux"]})


@host_app.command("create")
def host_create(
    host_kind: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Create or reuse a session for one agent."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
    _emit({
        "host_kind": host.kind,
        "agent_id": agent_id,
        "session_id": session.session_id,
        "workspace_ref": session.workspace_ref,
        "metadata": session.metadata,
    })


@host_app.command("exists")
def host_exists(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Check whether a session currently exists."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    _emit({
        "host_kind": host.kind,
        "session_id": session_id,
        "agent_id": agent_id,
        "exists": host.session_exists(
            _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
        ),
    })


@host_app.command("health")
def host_health(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Fetch session health."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    health = host.health(_session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref))
    _emit({
        "host_kind": host.kind,
        "session_id": health.session_id,
        "exists": health.exists,
        "healthy": health.healthy,
        "reason": health.reason,
        "metadata": health.metadata,
    })


@host_app.command("send")
def host_send(
    host_kind: str,
    session_id: str,
    agent_id: str,
    text: str,
    enter: bool = True,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Send text to an existing session."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    host.send_text(session, text, enter=enter)
    _emit({
        "host_kind": host.kind,
        "session_id": session.session_id,
        "agent_id": session.agent_id,
        "sent": True,
        "enter": enter,
        "text": text,
    })


@host_app.command("read")
def host_read(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Read visible output and one incremental output pass from a session."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    cursor = host.load_cursor(session) or host.create_cursor(session)
    read = host.read_output(session, cursor)
    visible_text = host.read_visible(session)
    _emit({
        "host_kind": host.kind,
        "session_id": session.session_id,
        "agent_id": session.agent_id,
        "changed": read.changed,
        "text": read.text,
        "full_text": read.full_text or visible_text,
        "visible_text": visible_text,
        "cursor_metadata": read.cursor.metadata,
    })


@host_app.command("snapshot")
def host_snapshot(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Capture a session snapshot."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    _emit(host.snapshot(session))


@host_app.command("interrupt")
def host_interrupt(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Interrupt a session."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    host.interrupt(session)
    _emit({"host_kind": host.kind, "session_id": session.session_id, "agent_id": session.agent_id, "interrupted": True})


@host_app.command("terminate")
def host_terminate(
    host_kind: str,
    session_id: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Terminate a session."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    session = _session(session_id=session_id, agent_id=agent_id, workspace_ref=workspace_ref)
    host.terminate_session(session)
    _emit({"host_kind": host.kind, "session_id": session.session_id, "agent_id": session.agent_id, "terminated": True})


@adapter_app.command("list-adapters")
def adapter_list_adapters() -> None:
    """List supported adapter kinds."""
    _emit({"items": ["default", "codex"]})


@adapter_app.command("bootstrap")
def adapter_bootstrap(
    adapter_kind: str,
    host_kind: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Create or reuse a session and bootstrap the adapter into it."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    adapter = build_agent_adapter(adapter_kind)
    session = host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
    claimed = {
        "agent_id": agent_id,
        "job": {"job_id": "local-bootstrap"},
        "run": {"run_id": "local-bootstrap"},
        "message": {"text": "bootstrap", "metadata": {"standalone": True}},
        "lease": {"lease_id": "local-bootstrap", "fencing_token": 1},
    }
    adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
    _emit({
        "host_kind": host.kind,
        "adapter_kind": adapter.kind,
        "session_id": session.session_id,
        "agent_id": agent_id,
        "metadata": session.metadata,
    })


@adapter_app.command("inspect")
def adapter_inspect(
    adapter_kind: str,
    path: str,
    run_id: str | None = None,
) -> None:
    """Inspect a transcript or raw output file through one adapter."""
    adapter = build_agent_adapter(adapter_kind)
    text = Path(path).read_text(encoding="utf-8")
    _emit(adapter.inspect_output(text=text, run_id=run_id))


@adapter_app.command("run-once")
def adapter_run_once(
    adapter_kind: str,
    host_kind: str,
    agent_id: str,
    task: str | None = None,
    task_file: str | None = None,
    workspace_ref: str | None = None,
    output_root: str = ".agp-plugin-runs",
    keep_session: bool = False,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Run one standalone task through a host and adapter."""
    runner = StandalonePluginRunner(
        host=build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace)),
        adapter=build_agent_adapter(adapter_kind),
        output_root=output_root,
    )
    result = runner.run_once(
        agent_id=agent_id,
        task=_read_task(task=task, task_file=task_file),
        workspace_ref=workspace_ref,
        keep_session=keep_session,
    )
    _emit(result.to_dict())


@plugin_app.command("run")
def plugin_run(
    host_kind: str,
    adapter_kind: str,
    agent_id: str,
    task: str | None = None,
    task_file: str | None = None,
    workspace_ref: str | None = None,
    output_root: str = ".agp-plugin-runs",
    keep_session: bool = False,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Run one standalone task through the shared plugin interfaces."""
    runner = StandalonePluginRunner(
        host=build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace)),
        adapter=build_agent_adapter(adapter_kind),
        output_root=output_root,
    )
    result = runner.run_once(
        agent_id=agent_id,
        task=_read_task(task=task, task_file=task_file),
        workspace_ref=workspace_ref,
        keep_session=keep_session,
    )
    _emit(result.to_dict())


@plugin_app.command("repl")
def plugin_repl(
    host_kind: str,
    adapter_kind: str,
    agent_id: str,
    workspace_ref: str | None = None,
    workspace: str | None = settings.wezterm_workspace,
) -> None:
    """Create or reuse a session, bootstrap it, and stream tasks from stdin until exit."""
    host = build_terminal_host(host_kind, **_host_kwargs(host_kind, workspace=workspace))
    adapter = build_agent_adapter(adapter_kind)
    session = host.get_or_create_session(agent_id=agent_id, workspace_ref=workspace_ref)
    claimed = {
        "agent_id": agent_id,
        "job": {"job_id": "local-repl"},
        "run": {"run_id": "local-repl"},
        "message": {"text": "bootstrap", "metadata": {"standalone": True, "repl": True}},
        "lease": {"lease_id": "local-repl", "fencing_token": 1},
    }
    adapter.ensure_bootstrapped(host=host, session=session, claimed=claimed)
    typer.echo(json.dumps({"host_kind": host.kind, "adapter_kind": adapter.kind, "session_id": session.session_id, "agent_id": agent_id}, sort_keys=True))
    while True:
        line = typer.prompt("task", prompt_suffix="> ", default="", show_default=False)
        if not line.strip():
            continue
        if line.strip() in {"/exit", "exit", "quit"}:
            break
        result = StandalonePluginRunner(host=host, adapter=adapter).run_once(
            agent_id=agent_id,
            task=line,
            workspace_ref=workspace_ref,
            keep_session=True,
        )
        _emit(result.to_dict())
