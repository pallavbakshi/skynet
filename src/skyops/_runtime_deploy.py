"""``skyops runtime deploy`` — generate deployment scripts for runtimes."""

from __future__ import annotations

import shlex
import textwrap
from urllib.parse import urlparse, urlunparse

import typer

from skyops.config import load_config
from skyops._client import resolve_server_url


def _join_shell(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def _env_prefix(env_vars: dict[str, str]) -> str:
    if not env_vars:
        return ""
    return " ".join(f"{key}={shlex.quote(value)}" for key, value in env_vars.items()) + " "


def _runtime_env(
    *,
    workspace_ref: str | None = None,
    artifact_backend: str = "http",
    runtime_token: str | None = None,
) -> dict[str, str]:
    env = {"AGP_ARTIFACT_BACKEND": artifact_backend}
    if runtime_token:
        env["AGP_RUNTIME_BEARER_TOKEN"] = runtime_token
    if workspace_ref:
        env["AGP_TMUX_DEFAULT_CWD"] = workspace_ref
        env["AGP_WEZTERM_DEFAULT_CWD"] = workspace_ref
    return env


def _systemd_env_line(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{key}={escaped}"'


def _prerequisite_checks(host_kind: str, adapter_kind: str) -> str:
    lines: list[str] = []
    if host_kind == "tmux":
        lines.extend([
            'if ! command -v tmux &>/dev/null; then',
            '    echo "ERROR: tmux is required but not installed." >&2',
            "    exit 1",
            "fi",
        ])
    elif host_kind == "wezterm":
        lines.extend([
            'if ! command -v wezterm &>/dev/null; then',
            '    echo "ERROR: wezterm is required but not installed." >&2',
            "    exit 1",
            "fi",
        ])
    if adapter_kind == "codex":
        lines.extend([
            'CODEX_BIN="${AGP_CODEX_CLI_COMMAND:-codex}"',
            'CODEX_BIN="${CODEX_BIN%% *}"',
            'if ! command -v "${CODEX_BIN}" &>/dev/null; then',
            '    echo "ERROR: Codex CLI is required but not installed." >&2',
            "    exit 1",
            "fi",
        ])
    return "\n".join(lines)


def _docker_reachable_server_url(server_url: str) -> tuple[str, bool]:
    parsed = urlparse(server_url)
    hostname = parsed.hostname or ""
    if hostname not in {"127.0.0.1", "localhost"}:
        return server_url, False
    netloc = "host.docker.internal"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc)), True


def _build_command(
    runtime_id: str,
    server_url: str,
    host_kind: str,
    adapter_kind: str,
    agent_id: str | None,
    env_vars: dict[str, str] | None = None,
) -> str:
    """Build the ``agp runtime-work-loop`` command string."""
    parts = [
        "agp",
        "runtime-work-loop",
        runtime_id,
        "--server-url",
        server_url,
        "--host-kind",
        host_kind,
        "--adapter-kind",
        adapter_kind,
    ]
    if agent_id:
        parts.extend(["--agent-id", agent_id])
    return _env_prefix(env_vars or {}) + _join_shell(parts)


def _build_docker_run(
    *,
    runtime_id: str,
    server_url: str,
    host_kind: str,
    adapter_kind: str,
    agent_id: str | None,
    runtime_token: str,
    image: str,
    workspace_ref: str | None,
    mounts: list[str],
    prepare_commands: list[str],
) -> str:
    """Build a docker run command for an interactive runtime."""
    reachable_server_url, needs_host_gateway = _docker_reachable_server_url(server_url)
    parts = [
        "docker", "run", "--rm", "-it",
        "--name", runtime_id,
        "-e", f"AGP_SERVER_URL={reachable_server_url}",
        "-e", f"AGP_RUNTIME_ID={runtime_id}",
        "-e", f"AGP_RUNTIME_TERMINAL_HOST_KIND={host_kind}",
        "-e", f"AGP_RUNTIME_AGENT_ADAPTER_KIND={adapter_kind}",
        "-e", "AGP_ARTIFACT_BACKEND=http",
        "-e", "OPENAI_API_KEY",
        "-e", "OPENROUTER_API_KEY",
        "-e", "OPENAI_BASE_URL",
        "-e", "ANTHROPIC_API_KEY",
    ]
    if needs_host_gateway:
        parts.extend(["--add-host", "host.docker.internal:host-gateway"])
    if runtime_token:
        parts.extend(["-e", f"AGP_RUNTIME_BEARER_TOKEN={runtime_token}"])
    if agent_id:
        parts.extend(["-e", f"AGP_RUNTIME_AGENT_ID={agent_id}"])
    if workspace_ref:
        parts.extend(["-e", f"AGP_TMUX_DEFAULT_CWD={workspace_ref}"])
        parts.extend(["-e", f"AGP_WEZTERM_DEFAULT_CWD={workspace_ref}"])
    for mount in mounts:
        parts.extend(["-v", mount])
    parts.append(image)
    lines = [_join_shell(parts[:4])]
    cursor = 4
    while cursor < len(parts) - 1:
        chunk = parts[cursor:cursor + 2]
        lines.append(_join_shell(chunk))
        cursor += 2
    if cursor == len(parts) - 1:
        lines.append(_join_shell([parts[-1]]))
    docker_run = " \\\n  ".join(lines)
    prepare_block = _build_prepare_script(prepare_commands)
    if not prepare_block:
        return docker_run
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        {prepare_block}\
        {docker_run}
    """)


def _build_prepare_script(prepare_commands: list[str]) -> str:
    if not prepare_commands:
        return ""
    lines = ["# --- Prepare workspace ---", *prepare_commands, ""]
    return "\n".join(lines)


def _build_script(
    runtime_id: str,
    server_url: str,
    host_kind: str,
    adapter_kind: str,
    agent_id: str | None,
    runtime_token: str,
    prepare_commands: list[str],
    workspace_ref: str | None,
) -> str:
    """Build a self-contained bash deployment script."""
    cmd = _build_command(
        runtime_id,
        server_url,
        host_kind,
        adapter_kind,
        agent_id,
        env_vars=_runtime_env(workspace_ref=workspace_ref, runtime_token=runtime_token),
    )

    # Parse host and port from server_url for env vars
    parsed = urlparse(server_url)
    agp_host = parsed.hostname or "127.0.0.1"
    agp_port = str(parsed.port or 7860)

    prereq_checks = _prerequisite_checks(host_kind, adapter_kind)
    prereq_block = f"{prereq_checks}\n" if prereq_checks else ""

    token_export = ""
    if runtime_token:
        token_export = f"export AGP_RUNTIME_BEARER_TOKEN={shlex.quote(runtime_token)}"

    provider_exports = textwrap.dedent("""\
        if [ -n "${OPENAI_API_KEY:-}" ]; then export OPENAI_API_KEY; fi
        if [ -n "${OPENROUTER_API_KEY:-}" ]; then export OPENROUTER_API_KEY; fi
        if [ -n "${OPENAI_BASE_URL:-}" ]; then export OPENAI_BASE_URL; fi
        if [ -n "${ANTHROPIC_API_KEY:-}" ]; then export ANTHROPIC_API_KEY; fi
    """)

    prepare_block = _build_prepare_script(prepare_commands)

    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail

        # --- Prerequisites ---
        if ! command -v python3 &>/dev/null; then
            echo "ERROR: python3 is required but not installed." >&2
            exit 1
        fi

        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{{sys.version_info.major}}.{{sys.version_info.minor}}")')
        PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
        PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
        if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 12 ]); then
            echo "ERROR: Python 3.12+ is required (found $PYTHON_VERSION)." >&2
            exit 1
        fi

        if ! python3 -m pip --version &>/dev/null; then
            echo "ERROR: python3 -m pip is required but not installed." >&2
            exit 1
        fi

        {prereq_block}# --- Install agp ---
        python3 -m pip install 'agp[server]'
        # NOTE: If using a custom package index, replace the line above with:
        #   python3 -m pip install 'agp[server]' --index-url https://your-custom-index/simple/

        # --- Environment variables ---
        export AGP_HOST={shlex.quote(agp_host)}
        export AGP_PORT={shlex.quote(agp_port)}
        export AGP_ARTIFACT_BACKEND=http
        {token_export}
        {provider_exports}
        if [ -n "{workspace_ref or ''}" ]; then export AGP_TMUX_DEFAULT_CWD={shlex.quote(workspace_ref or '')}; fi
        if [ -n "{workspace_ref or ''}" ]; then export AGP_WEZTERM_DEFAULT_CWD={shlex.quote(workspace_ref or '')}; fi

        {prepare_block}\
        # --- Run the work loop ---
        exec {cmd}
    """)


def _build_systemd(
    runtime_id: str,
    server_url: str,
    host_kind: str,
    adapter_kind: str,
    agent_id: str | None,
    runtime_token: str,
    workspace_ref: str | None,
    prepare_commands: list[str],
) -> str:
    """Build a systemd unit file."""
    cmd = _build_command(runtime_id, server_url, host_kind, adapter_kind, agent_id)

    parsed = urlparse(server_url)
    agp_host = parsed.hostname or "127.0.0.1"
    agp_port = str(parsed.port or 7860)

    env_lines = [
        _systemd_env_line("AGP_HOST", agp_host),
        _systemd_env_line("AGP_PORT", agp_port),
        _systemd_env_line("AGP_ARTIFACT_BACKEND", "http"),
        "PassEnvironment=OPENAI_API_KEY OPENROUTER_API_KEY OPENAI_BASE_URL ANTHROPIC_API_KEY",
    ]
    if runtime_token:
        env_lines.append(_systemd_env_line("AGP_RUNTIME_BEARER_TOKEN", runtime_token))
    if workspace_ref:
        env_lines.append(_systemd_env_line("AGP_TMUX_DEFAULT_CWD", workspace_ref))
        env_lines.append(_systemd_env_line("AGP_WEZTERM_DEFAULT_CWD", workspace_ref))

    env_block = "\n".join(env_lines)
    exec_start_pre = "\n".join(
        f"ExecStartPre=/bin/sh -lc {shlex.quote(command)}" for command in prepare_commands
    )
    if exec_start_pre:
        exec_start_pre += "\n"

    return textwrap.dedent(f"""\
        [Unit]
        Description=AGP Runtime {runtime_id}
        After=network.target

        [Service]
        Type=simple
        {exec_start_pre}ExecStart={cmd}
        Restart=on-failure
        RestartSec=5
        {env_block}

        [Install]
        WantedBy=multi-user.target
    """)


def register_deploy_command(runtime_app: typer.Typer) -> None:
    """Register the deploy command on the given Typer app."""

    @runtime_app.command("deploy")
    def runtime_deploy(
        runtime_id: str = typer.Argument(help="Runtime ID to deploy."),
        server_url: str | None = typer.Option(None, "--server-url", help="Server URL (defaults to config)."),
        host_kind: str | None = typer.Option(None, "--host-kind", help="Terminal host kind (defaults to config runtime.host_kind)."),
        adapter_kind: str | None = typer.Option(None, "--adapter-kind", help="Agent adapter kind (defaults to config runtime.adapter_kind)."),
        agent_id: str | None = typer.Option(None, "--agent-id", help="Agent ID to bind to."),
        host_profile: str | None = typer.Option(None, "--host-profile", help="Host profile name for resolving mounts and git/worktree roots."),
        image: str = typer.Option("agp-runtime:latest", "--image", help="Runtime image to use for docker-run format."),
        fmt: str = typer.Option("command", "--format", help="Output format: command, script, systemd, or docker-run."),
    ) -> None:
        """Generate a ready-to-run deployment script for a runtime."""
        cfg = load_config()
        resolved_url = server_url or resolve_server_url(cfg)
        resolved_host_kind = host_kind or cfg.runtime.host_kind
        resolved_adapter_kind = adapter_kind or cfg.runtime.adapter_kind
        runtime_token = cfg.security.runtime_token
        workspace_ref: str | None = None
        mounts: list[str] = []
        prepare_commands: list[str] = []
        if agent_id:
            try:
                workspace = cfg.resolve_agent_workspace(agent_id, host_profile=host_profile)
            except (KeyError, ValueError) as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(1) from exc
            workspace_ref = workspace["workspace_ref"]
            mounts = workspace["mounts"]
            prepare_commands = workspace["prepare_commands"]

        if fmt == "command":
            typer.echo(
                _build_command(
                    runtime_id, resolved_url, resolved_host_kind,
                    resolved_adapter_kind, agent_id,
                    env_vars=_runtime_env(workspace_ref=workspace_ref, runtime_token=runtime_token),
                )
            )
        elif fmt == "script":
            typer.echo(
                _build_script(
                    runtime_id, resolved_url, resolved_host_kind,
                    resolved_adapter_kind, agent_id, runtime_token, prepare_commands, workspace_ref,
                )
            )
        elif fmt == "systemd":
            typer.echo(
                _build_systemd(
                    runtime_id, resolved_url, resolved_host_kind,
                    resolved_adapter_kind, agent_id, runtime_token, workspace_ref, prepare_commands,
                )
            )
        elif fmt == "docker-run":
            typer.echo(
                _build_docker_run(
                    runtime_id=runtime_id,
                    server_url=resolved_url,
                    host_kind=resolved_host_kind,
                    adapter_kind=resolved_adapter_kind,
                    agent_id=agent_id,
                    runtime_token=runtime_token,
                    image=image,
                    workspace_ref=workspace_ref,
                    mounts=mounts,
                    prepare_commands=prepare_commands,
                )
            )
        else:
            typer.echo(f"Unknown format: {fmt}. Use command, script, systemd, or docker-run.", err=True)
            raise typer.Exit(1)
