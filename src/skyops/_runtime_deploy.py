"""``skyops runtime deploy`` — generate deployment scripts for runtimes."""

from __future__ import annotations

import textwrap

import typer

from skyops.config import load_config
from skyops._client import resolve_server_url


def _build_command(
    runtime_id: str,
    server_url: str,
    host_kind: str,
    adapter_kind: str,
    agent_id: str | None,
) -> str:
    """Build the ``agp runtime-work-loop`` command string."""
    parts = [
        "agp",
        "runtime-work-loop",
        runtime_id,
        f"--server-url {server_url}",
        f"--host-kind {host_kind}",
        f"--adapter-kind {adapter_kind}",
    ]
    if agent_id:
        parts.append(f"--agent-id {agent_id}")
    return " ".join(parts)


def _build_script(
    runtime_id: str,
    server_url: str,
    host_kind: str,
    adapter_kind: str,
    agent_id: str | None,
    runtime_token: str,
) -> str:
    """Build a self-contained bash deployment script."""
    cmd = _build_command(runtime_id, server_url, host_kind, adapter_kind, agent_id)

    # Parse host and port from server_url for env vars
    from urllib.parse import urlparse

    parsed = urlparse(server_url)
    agp_host = parsed.hostname or "127.0.0.1"
    agp_port = str(parsed.port or 7860)

    tmux_note = ""
    if host_kind == "tmux":
        tmux_note = textwrap.dedent("""\
            # NOTE: tmux is required for host-kind=tmux
            if ! command -v tmux &>/dev/null; then
                echo "ERROR: tmux is required but not installed." >&2
                exit 1
            fi
        """)

    token_export = ""
    if runtime_token:
        token_export = f'export AGP_RUNTIME_BEARER_TOKEN="{runtime_token}"'

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

        {tmux_note}# --- Install agp ---
        pip install agp
        # NOTE: If using a custom package index, replace the line above with:
        #   pip install agp --index-url https://your-custom-index/simple/

        # --- Environment variables ---
        export AGP_HOST="{agp_host}"
        export AGP_PORT="{agp_port}"
        {token_export}

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
) -> str:
    """Build a systemd unit file."""
    cmd = _build_command(runtime_id, server_url, host_kind, adapter_kind, agent_id)

    from urllib.parse import urlparse

    parsed = urlparse(server_url)
    agp_host = parsed.hostname or "127.0.0.1"
    agp_port = str(parsed.port or 7860)

    env_lines = [
        f"Environment=AGP_HOST={agp_host}",
        f"Environment=AGP_PORT={agp_port}",
    ]
    if runtime_token:
        env_lines.append(f"Environment=AGP_RUNTIME_BEARER_TOKEN={runtime_token}")

    env_block = "\n".join(env_lines)

    return textwrap.dedent(f"""\
        [Unit]
        Description=AGP Runtime {runtime_id}
        After=network.target

        [Service]
        Type=simple
        ExecStart={cmd}
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
        fmt: str = typer.Option("command", "--format", help="Output format: command, script, or systemd."),
    ) -> None:
        """Generate a ready-to-run deployment script for a runtime."""
        cfg = load_config()
        resolved_url = server_url or resolve_server_url(cfg)
        resolved_host_kind = host_kind or cfg.runtime.host_kind
        resolved_adapter_kind = adapter_kind or cfg.runtime.adapter_kind
        runtime_token = cfg.security.runtime_token

        if fmt == "command":
            typer.echo(
                _build_command(
                    runtime_id, resolved_url, resolved_host_kind,
                    resolved_adapter_kind, agent_id,
                )
            )
        elif fmt == "script":
            typer.echo(
                _build_script(
                    runtime_id, resolved_url, resolved_host_kind,
                    resolved_adapter_kind, agent_id, runtime_token,
                )
            )
        elif fmt == "systemd":
            typer.echo(
                _build_systemd(
                    runtime_id, resolved_url, resolved_host_kind,
                    resolved_adapter_kind, agent_id, runtime_token,
                )
            )
        else:
            typer.echo(f"Unknown format: {fmt}. Use command, script, or systemd.", err=True)
            raise typer.Exit(1)
