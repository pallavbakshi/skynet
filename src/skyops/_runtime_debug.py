"""``skyops runtime`` — runtime debugging commands."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess

import typer

from skyops._client import build_client, build_profile
from skyops.config import load_config

runtime_debug_app = typer.Typer(help="Runtime debugging commands.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _runtime_bearer_token(cfg) -> str | None:
    token = os.environ.get("AGP_RUNTIME_BEARER_TOKEN")
    if token:
        return token
    return cfg.security.runtime_token or None


@runtime_debug_app.command("list")
def runtime_list(
    status: str | None = typer.Option(None, "--status", help="Filter by runtime status."),
    health_status: str | None = typer.Option(None, "--health", help="Filter by runtime health status."),
    limit: int = typer.Option(50, "--limit", "-l", help="Max results."),
    cursor: str | None = typer.Option(None, "--cursor", help="Pagination cursor."),
) -> None:
    """List runtimes known to the control plane."""
    with build_client() as client:
        result = client.list_runtimes(status=status, health_status=health_status, limit=limit, cursor=cursor)
    _emit(result)


@runtime_debug_app.command("inspect")
def runtime_inspect(
    runtime_id: str = typer.Argument(help="Runtime ID to inspect."),
) -> None:
    """Show detailed runtime state."""
    with build_client() as client:
        result = client.get_runtime(runtime_id)
    if result is None:
        typer.echo(f"Runtime not found: {runtime_id}", err=True)
        raise typer.Exit(1)
    _emit(result)


@runtime_debug_app.command("register")
def runtime_register(
    runtime_id: str = typer.Argument(help="Runtime ID to register."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname (defaults to system hostname)."),
) -> None:
    """Register a runtime with the control plane (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity

    cfg = load_config()
    actual_hostname = hostname or socket.gethostname()
    runtime_token = _runtime_bearer_token(cfg)
    server_url = build_profile(cfg).server_url
    identity = RuntimeIdentity(
        runtime_id=runtime_id,
        hostname=actual_hostname,
        server_url=server_url,
        token=runtime_token,
    )
    client = RuntimeClient(identity)
    try:
        result = client.register()
        _emit(result)
    finally:
        client.close()


@runtime_debug_app.command("claim")
def runtime_claim(
    runtime_id: str = typer.Argument(help="Runtime ID."),
    agent_id: str = typer.Argument(help="Agent ID to claim work for."),
    lease_ttl: int = typer.Option(30, "--lease-ttl", help="Lease TTL in seconds."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname."),
) -> None:
    """Claim a single job for a runtime (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity

    cfg = load_config()
    actual_hostname = hostname or socket.gethostname()
    runtime_token = _runtime_bearer_token(cfg)
    server_url = build_profile(cfg).server_url
    identity = RuntimeIdentity(
        runtime_id=runtime_id,
        hostname=actual_hostname,
        server_url=server_url,
        token=runtime_token,
    )
    client = RuntimeClient(identity)
    try:
        result = client.claim(agent_id=agent_id, lease_ttl_seconds=lease_ttl)
        _emit(result)
    finally:
        client.close()


@runtime_debug_app.command("work-once")
def runtime_work_once(
    runtime_id: str = typer.Argument(help="Runtime ID."),
    agent_id: str | None = typer.Option(None, "--agent-id", help="Agent ID."),
    capability_id: str | None = typer.Option(None, "--capability-id", help="Capability ID."),
    server_url: str | None = typer.Option(None, "--server-url", help="Server URL (defaults to config)."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname."),
    artifact_root: str = typer.Option(".agp-artifacts", "--artifact-root", help="Artifact root."),
    host_kind: str = typer.Option("inprocess", "--host-kind", help="Terminal host kind."),
    adapter_kind: str = typer.Option("default", "--adapter-kind", help="Agent adapter kind."),
) -> None:
    """Run a single iteration of the runtime work loop (debug)."""
    from agp.client import RuntimeClient, RuntimeIdentity
    from agp.plugins import build_agent_adapter, build_terminal_host
    from agp.runtime import RuntimeSupervisor

    cfg = load_config()
    resolved_url = server_url or build_profile(cfg).server_url
    actual_hostname = hostname or socket.gethostname()
    runtime_token = _runtime_bearer_token(cfg)
    client = RuntimeClient(
        RuntimeIdentity(
            runtime_id=runtime_id,
            hostname=actual_hostname,
            server_url=resolved_url,
            token=runtime_token,
        )
    )
    worker = RuntimeSupervisor(
        client,
        host=build_terminal_host(host_kind),
        adapter=build_agent_adapter(adapter_kind),
        artifact_root=artifact_root,
    )
    from threading import Event

    stop_event = Event()
    try:
        payload = worker.run_forever(
            agent_id=agent_id,
            capability_id=capability_id,
            idle_sleep_seconds=0.25,
            max_iterations=1,
            stop_event=stop_event,
        )
    finally:
        stop_event.set()
        client.close()
    _emit(payload)


@runtime_debug_app.command("work-loop")
def runtime_work_loop(
    runtime_id: str = typer.Argument(help="Runtime ID."),
    server_url: str | None = typer.Option(None, "--server-url", help="Server URL (defaults to config)."),
    hostname: str | None = typer.Option(None, "--hostname", help="Hostname."),
    agent_id: str | None = typer.Option(None, "--agent-id", help="Agent ID."),
    capability_id: str | None = typer.Option(None, "--capability-id", help="Capability ID."),
    artifact_root: str = typer.Option(".agp-artifacts", "--artifact-root", help="Artifact root."),
    idle_sleep_seconds: float = typer.Option(0.25, "--idle-sleep-seconds", help="Idle poll sleep."),
    max_iterations: int | None = typer.Option(None, "--max-iterations", help="Stop after N iterations."),
    max_local_recoveries: int = typer.Option(1, "--max-local-recoveries", help="Max local recovery attempts."),
    host_kind: str | None = typer.Option(None, "--host-kind", help="Terminal host kind."),
    adapter_kind: str | None = typer.Option(None, "--adapter-kind", help="Agent adapter kind."),
) -> None:
    """Run the continuous runtime work loop."""
    from agp.cli import runtime_work_loop as agp_runtime_work_loop

    cfg = load_config()
    resolved_url = server_url or build_profile(cfg).server_url
    had_previous_token = "AGP_RUNTIME_BEARER_TOKEN" in os.environ
    previous_token = os.environ.get("AGP_RUNTIME_BEARER_TOKEN")
    configured_token = _runtime_bearer_token(cfg)
    if not previous_token and configured_token:
        os.environ["AGP_RUNTIME_BEARER_TOKEN"] = configured_token
    try:
        agp_runtime_work_loop(
            runtime_id=runtime_id,
            server_url=resolved_url,
            hostname=hostname,
            agent_id=agent_id,
            capability_id=capability_id,
            artifact_root=artifact_root,
            idle_sleep_seconds=idle_sleep_seconds,
            max_iterations=max_iterations,
            max_local_recoveries=max_local_recoveries,
            host_kind=host_kind,
            adapter_kind=adapter_kind,
        )
    finally:
        if not had_previous_token:
            os.environ.pop("AGP_RUNTIME_BEARER_TOKEN", None)
        elif previous_token != os.environ.get("AGP_RUNTIME_BEARER_TOKEN"):
            os.environ["AGP_RUNTIME_BEARER_TOKEN"] = previous_token or ""


_DEFAULT_VOLUME = "agp-credentials"
_CRED_MOUNT = "/credentials"


@runtime_debug_app.command("env")
def runtime_env(
    key: str | None = typer.Argument(None, help="Variable name. Omit to list all."),
    value: str | None = typer.Argument(None, help="Value to set. Omit to show current value."),
    unset: bool = typer.Option(False, "--unset", "-d", help="Remove the variable."),
    file: str | None = typer.Option(None, "--file", "-f", help="Import a local .env file (replaces all vars)."),
    runtime_id: str | None = typer.Option(None, "--runtime-id", "-r", help="Target a specific runtime (per-container override)."),
    volume: str = typer.Option(_DEFAULT_VOLUME, "--volume", help="Docker volume."),
    image: str = typer.Option("agp-runtime-test:latest", "--image", help="Runtime image."),
) -> None:
    """Manage shared environment variables on the credentials volume.

    Variables are stored in /credentials/.env and sourced by every
    container at startup.  Use --runtime-id to set per-container
    overrides (stored in /credentials/.env.<runtime-id>).

    Examples:
        skyops runtime env                                  # list shared vars
        skyops runtime env OPENAI_API_KEY sk-...            # set shared var
        skyops runtime env OPENAI_API_KEY -r my-runtime     # set for one runtime only
        skyops runtime env OPENAI_API_KEY --unset           # remove a var
        skyops runtime env --file .env                      # import a local .env file
        skyops runtime env --file .env -r my-runtime        # import for one runtime
    """
    docker = shutil.which("docker")
    if not docker:
        typer.echo("docker not found in PATH", err=True)
        raise typer.Exit(1)

    suffix = f".{runtime_id}" if runtime_id else ""
    env_path = f"{_CRED_MOUNT}/.env{suffix}"
    label = f" (runtime={runtime_id})" if runtime_id else " (shared)"
    mount = ["-v", f"{volume}:{_CRED_MOUNT}"]

    def _run_in_vol(cmd: str, *, stdin: str | None = None) -> str:
        r = subprocess.run(
            [docker, "run", "--rm", "-i", *mount, image, "sh", "-c", cmd],
            input=stdin, capture_output=True, text=True, check=False,
        )
        return r.stdout

    # Import from file
    if file is not None:
        import pathlib
        content = pathlib.Path(file).read_text()
        _run_in_vol(f"cat > {env_path}", stdin=content)
        count = sum(1 for ln in content.splitlines() if ln.strip() and not ln.startswith("#"))
        typer.echo(f"✓ Imported {count} variable(s) from {file}{label}")
        return

    # List all
    if key is None:
        out = _run_in_vol(f"cat {env_path} 2>/dev/null || true")
        typer.echo(out or f"(empty){label}")
        return

    # Unset
    if unset:
        _run_in_vol(
            f"touch {env_path} && "
            f"grep -v '^{key}=' {env_path} > {env_path}.tmp && "
            f"mv {env_path}.tmp {env_path}"
        )
        typer.echo(f"✓ Unset {key}{label}")
        return

    # Show single var
    if value is None:
        out = _run_in_vol(f"grep '^{key}=' {env_path} 2>/dev/null || true")
        typer.echo(out.strip() or f"{key} not set{label}")
        return

    # Set var — remove old entry, append new one
    import shlex
    safe_value = shlex.quote(value)
    _run_in_vol(
        f"touch {env_path} && "
        f"grep -v '^{key}=' {env_path} > {env_path}.tmp 2>/dev/null; "
        f"echo '{key}='{safe_value} >> {env_path}.tmp && "
        f"mv {env_path}.tmp {env_path}"
    )
    typer.echo(f"✓ {key} set{label}")


_TOOL_AUTH: dict[str, dict] = {
    "claude": {
        "auth_cmd": "claude --dangerously-skip-permissions",
        "home_dir": ".claude",
        "verify_file": ".credentials.json",
        "description": "Claude Code OAuth",
        "post_auth_copy": (".claude.json", ".claude.json"),  # $HOME file → volume subdir
    },
    "codex": {
        "auth_cmd": "codex",
        "home_dir": ".codex",
        "verify_file": None,
        "description": "Codex setup",
        "post_auth_copy": None,
    },
}


@runtime_debug_app.command("auth")
def runtime_auth(
    tool: str = typer.Argument("claude", help=f"Tool to authenticate: {', '.join(_TOOL_AUTH)}"),
    image: str = typer.Option("agp-runtime-test:latest", "--image", help="Runtime image."),
    volume: str = typer.Option("agp-credentials", "--volume", help="Docker volume for shared credentials."),
    user: str = typer.Option("pb", "--user", "-u", help="Container user."),
    hostname: str = typer.Option("agp-runtime", "--hostname", help="Container hostname (must match runtime identity)."),
    mac: str = typer.Option("02:42:37:fc:f5:93", "--mac", help="Container MAC (must match runtime identity)."),
) -> None:
    """Run interactive auth setup for a tool (claude, codex).

    Launches a temporary container with the credentials volume,
    runs the tool's auth flow, and persists credentials into the
    shared volume under /credentials/<tool>/.

    Examples:
        skyops runtime auth claude   # OAuth for Claude Code
        skyops runtime auth codex    # First-run setup for Codex
    """
    import shlex

    if tool not in _TOOL_AUTH:
        typer.echo(f"Unknown tool: {tool}. Supported: {', '.join(_TOOL_AUTH)}", err=True)
        raise typer.Exit(1)

    spec = _TOOL_AUTH[tool]
    docker = shutil.which("docker")
    if not docker:
        typer.echo("docker not found in PATH", err=True)
        raise typer.Exit(1)

    safe_user = shlex.quote(user)
    home = f"/home/{user}"
    cred_root = "/credentials"
    tool_dir = f"{cred_root}/{tool}"

    typer.echo(f"Starting {spec['description']} (volume={volume}, tool={tool})...")
    typer.echo("Complete the auth flow, then exit when done.\n")

    # Build the in-container bash script:
    # 1. Create user, tool subdir, symlink into $HOME
    # 2. Run tool's auth command
    # 3. Post-auth copy (e.g. .claude.json)
    setup = (
        f"useradd -m {safe_user} 2>/dev/null; "
        f"mkdir -p {tool_dir}; "
        f"chown -R {safe_user}:{safe_user} {tool_dir}; "
        f"ln -sfn {tool_dir} {home}/{spec['home_dir']}; "
    )
    auth = f"runuser -u {safe_user} -- env HOME={home} {spec['auth_cmd']}; EC=$?; "
    post = ""
    if spec.get("post_auth_copy"):
        src_name, dst_name = spec["post_auth_copy"]
        post = (
            f"if [ -f {home}/{src_name} ]; then "
            f"  cp {home}/{src_name} {tool_dir}/{dst_name}; "
            f"else "
            f'  echo "WARNING: {home}/{src_name} not found" >&2; '
            f"fi; "
        )

    result = subprocess.run([
        docker, "run", "-it", "--rm",
        "--hostname", hostname,
        "--mac-address", mac,
        "-v", f"{volume}:{cred_root}",
        image,
        "bash", "-c", setup + auth + post + "exit $EC",
    ], check=False)

    if result.returncode != 0:
        typer.echo(f"\n✗ Exited with code {result.returncode}.", err=True)
        raise typer.Exit(result.returncode)

    # Verify tool-specific credential file if defined
    if spec.get("verify_file"):
        check = subprocess.run(
            [docker, "run", "--rm", "-v", f"{volume}:{cred_root}",
             image, "test", "-s", f"{tool_dir}/{spec['verify_file']}"],
            check=False,
        )
        if check.returncode != 0:
            typer.echo(f"\n✗ No credentials found at {tool_dir}/{spec['verify_file']}.", err=True)
            typer.echo("  The auth flow may not have completed. Run again.", err=True)
            raise typer.Exit(1)

    # Claude-specific: verify onboarding state
    if tool == "claude" and spec.get("post_auth_copy"):
        _, dst_name = spec["post_auth_copy"]
        check_onboarding = subprocess.run(
            [docker, "run", "--rm", "-v", f"{volume}:{cred_root}",
             image, "test", "-s", f"{tool_dir}/{dst_name}"],
            check=False,
        )
        if check_onboarding.returncode != 0:
            typer.echo("\n⚠ Credentials saved but onboarding state (.claude.json) missing.", err=True)
            typer.echo("  First-run screens may appear on next launch.", err=True)
        else:
            typer.echo("✓ Onboarding state persisted.")

    typer.echo(f"✓ {spec['description']} credentials saved to volume.")

    # Write sentinel so the entrypoint knows this is a real initialized volume
    subprocess.run(
        [docker, "run", "--rm", "-v", f"{volume}:{cred_root}",
         image, "sh", "-c", f"date -Iseconds > {cred_root}/.volume-initialized"],
        check=False,
    )
    typer.echo("✓ Volume initialized. Ready for runtime containers.")


@runtime_debug_app.command("attach")
def runtime_attach(
    container: str = typer.Argument(
        "agp-runtime-live",
        help="Docker container name or ID.",
    ),
    host_kind: str = typer.Option(
        "wezterm",
        "--host-kind",
        help="Terminal host kind: wezterm or tmux.",
    ),
    user: str = typer.Option("pb", "--user", "-u", help="Container user."),
    pane_id: str | None = typer.Option(None, "--pane-id", help="Specific pane/session to attach."),
    peek: bool = typer.Option(False, "--peek", "-p", help="Print current screen and exit."),
    domain: str = typer.Option("agp", "--domain", "-d", help="WezTerm unix_domain name from your local config."),
) -> None:
    """Attach to a running runtime container's terminal session.

    For WezTerm: requires a unix_domain in your local wezterm config
    with a proxy_command that docker-execs into the container. The
    default domain name is 'agp'.  Example wezterm.lua entry::

        { name = 'agp', proxy_command = { 'docker', 'exec', '-i',
          'agp-runtime-live', 'runuser', '-u', 'pb', '--',
          'env', 'HOME=/home/pb',
          'WEZTERM_CONFIG_FILE=/etc/wezterm/wezterm.lua',
          'wezterm', 'cli', 'proxy' } }
    """
    docker = shutil.which("docker")
    if not docker:
        typer.echo("docker not found in PATH", err=True)
        raise typer.Exit(1)

    wez_env = ["env", "WEZTERM_CONFIG_FILE=/etc/wezterm/wezterm.lua"]

    def _docker_exec(args: list[str], *, interactive: bool = False) -> subprocess.CompletedProcess[str]:
        flags = ["-i"] if interactive else []
        return subprocess.run(
            [docker, "exec", *flags, container, "runuser", "-u", user, "--",
             "env", f"HOME=/home/{user}", *args],
            capture_output=not interactive, text=True, check=False,
        )

    if host_kind == "tmux":
        if peek:
            # Auto-find the tmux session name
            target = pane_id
            if not target:
                result = _docker_exec(["tmux", "list-sessions", "-F", "#{session_name}"])
                sessions = [s.strip() for s in (result.stdout or "").splitlines() if s.strip()]
                # Prefer agp-* sessions over default ones
                target = next((s for s in sessions if s.startswith("agp-")), sessions[0] if sessions else "0")
            # -S -50 captures scrollback (TUI apps use alternate buffer)
            result = _docker_exec(["tmux", "capture-pane", "-t", target, "-p", "-S", "-50"])
            typer.echo(result.stdout)
        else:
            os.execvp(docker, [
                docker, "exec", "-it", container, "runuser", "-u", user, "--",
                "env", f"HOME=/home/{user}",
                "tmux", "attach-session", *(["-t", pane_id] if pane_id else []),
            ])
        return

    # WezTerm peek: show pane list + screen content
    if peek:
        result = _docker_exec([*wez_env, "wezterm", "cli", "list"])
        typer.echo(result.stdout)
        target = pane_id
        if not target:
            # Auto-find the agent pane (title starts with AGP: or has TUI markers)
            list_result = _docker_exec([*wez_env, "wezterm", "cli", "list", "--format", "json"])
            try:
                panes = json.loads(list_result.stdout or "[]")
                for p in panes:
                    title = p.get("tab_title", "") or p.get("window_title", "")
                    if title.startswith("AGP:") or "\u2733" in title:
                        target = str(p["pane_id"])
                        break
            except (json.JSONDecodeError, KeyError):
                pass
            target = target or "0"
        result = _docker_exec([*wez_env, "wezterm", "cli", "get-text", "--pane-id", target, "--start-line", "-50"])
        typer.echo(result.stdout)
        return

    # WezTerm full GUI attach via preconfigured unix_domain
    local_wezterm = shutil.which("wezterm")
    if not local_wezterm:
        typer.echo("wezterm not found locally — install WezTerm or use --peek", err=True)
        raise typer.Exit(1)

    typer.echo(f"Connecting to {container} via wezterm domain '{domain}'...")
    os.execvp(local_wezterm, [local_wezterm, "connect", domain])
