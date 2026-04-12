"""``skyops up``, ``skyops down``, ``skyops restart`` — service lifecycle."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import typer

from skyops.config import SkyopsConfig, build_agp_env, load_config
from skyops._client import resolve_server_url, _connectable_host
from skyops._status import _probe_tcp, _probe_http_health
from skyops._pidfile import pid_dir, write_pidfile, list_pidfiles, signal_and_wait

lifecycle_app = typer.Typer(help="Manage AGP stack services.")

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib  # type: ignore[no-redef]

# ── Docker mode helpers ─────────────────────────────────────────────


def _compose_cmd(cfg: SkyopsConfig) -> list[str]:
    """Return the base docker compose command with project/file flags."""
    return [
        "docker", "compose",
        "-f", cfg.stack.compose_file,
        "-p", cfg.stack.project_name,
    ]


def _is_first_boot(cfg: SkyopsConfig) -> bool:
    """Check if this is the first boot by looking for a marker file."""
    marker = Path(cfg._config_path.parent / ".skyops-initialized") if cfg._config_path else Path(".skyops-initialized")
    return not marker.exists()


def _mark_initialized(cfg: SkyopsConfig) -> None:
    """Write a marker file indicating the stack has been initialized."""
    marker = Path(cfg._config_path.parent / ".skyops-initialized") if cfg._config_path else Path(".skyops-initialized")
    marker.write_text("initialized\n")


def _docker_up(cfg: SkyopsConfig, service: str | None, build: bool) -> None:
    cmd = _compose_cmd(cfg) + ["up", "-d"]
    if build:
        cmd.append("--build")
    if service:
        cmd.append(service)
    typer.echo(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _docker_down(cfg: SkyopsConfig, service: str | None, volumes: bool) -> None:
    cmd = _compose_cmd(cfg) + ["down"]
    if volumes:
        cmd.extend(["-v", "--remove-orphans"])
    if service:
        cmd = _compose_cmd(cfg) + ["stop", service]
    typer.echo(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


# ── Bare-metal mode helpers ────────────────────────────────────────

def _pid_directory(cfg: SkyopsConfig) -> Path:
    """Return the PID directory derived from the config file location."""
    base = cfg._config_path.parent if cfg._config_path else Path.cwd()
    return pid_dir(base)


def _start_bg(
    cmd: list[str],
    label: str,
    pid_directory: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    typer.echo(f"Starting {label}: {' '.join(cmd)}")
    log_path = None
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    if pid_directory is not None:
        log_path = pid_directory / f"{label}.log"
        stdout = open(log_path, "a")  # noqa: SIM115
        stderr = subprocess.STDOUT
    proc = subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        env=env,
        start_new_session=True,
    )
    if pid_directory is not None:
        write_pidfile(pid_directory, label, proc.pid)
    return proc


def _agp_process_env(cfg: SkyopsConfig) -> dict[str, str]:
    env = build_agp_env(cfg)
    env["AGP_REDIS_URL"] = cfg.redis.url
    env["AGP_S3_ENDPOINT_URL"] = cfg.s3.endpoint_url
    env["AGP_S3_ACCESS_KEY_ID"] = cfg.s3.access_key_id
    env["AGP_S3_SECRET_ACCESS_KEY"] = cfg.s3.secret_access_key
    env["AGP_S3_BUCKET"] = cfg.s3.bucket
    return env


def _wait_tcp(host: str, port: int, label: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _probe_tcp(host, port):
            typer.echo(f"  {label} ready on :{port}")
            return
        time.sleep(0.5)
    raise RuntimeError(f"{label} did not become ready on :{port} within {timeout}s")


def _wait_http(url: str, label: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _probe_http_health(url):
            typer.echo(f"  {label} healthy at {url}")
            return
        time.sleep(0.5)
    raise RuntimeError(f"{label} did not become healthy at {url} within {timeout}s")


def _bare_metal_start_service(cfg: SkyopsConfig, service: str, host: str, port: int) -> None:
    """Start a single bare-metal service by name."""
    server_url = resolve_server_url(cfg)
    pdir = _pid_directory(cfg)
    agp_env = _agp_process_env(cfg)
    service_map = {
        "control-plane": lambda: (
            _start_bg(["agp", "serve", "--host", host, "--port", str(port)], "control-plane", pdir, env=agp_env),
            _wait_http(f"{server_url}/health", "control-plane"),
        ),
        "lease-sweeper": lambda: _start_bg(
            ["agp", "sweep-loop", "--interval-seconds", "5"], "lease-sweeper", pdir, env=agp_env,
        ),
        "runtime-sweeper": lambda: _start_bg(
            ["agp", "sweep-runtimes-loop", "--interval-seconds", "10"], "runtime-sweeper", pdir, env=agp_env,
        ),
        "runtime": lambda: _start_bg(
            ["agp", "runtime-work-loop", "rtm_local",
             "--server-url", server_url,
             "--agent-id", next(iter(cfg.agents), "agt_local"),
             "--host-kind", cfg.runtime.host_kind,
             "--adapter-kind", cfg.runtime.adapter_kind],
            "runtime", pdir, env=agp_env,
        ),
        "postgres": lambda: typer.echo("postgres must be started externally in bare-metal mode"),
        "redis": lambda: typer.echo("redis must be started externally in bare-metal mode"),
        "minio": lambda: typer.echo("minio must be started externally in bare-metal mode"),
    }
    # Also accept aliases
    service_map["sweep-loop"] = service_map["lease-sweeper"]
    service_map["sweep-runtimes-loop"] = service_map["runtime-sweeper"]

    starter = service_map.get(service)
    if starter is None:
        typer.echo(f"Unknown service: {service}", err=True)
        typer.echo(f"Available: {', '.join(sorted(service_map))}")
        raise typer.Exit(1)
    starter()


def _bare_metal_up(cfg: SkyopsConfig, service: str | None) -> None:
    """Start bare-metal services. If *service* is given, start only that one."""
    port = cfg.server.port
    host = cfg.server.host

    if service:
        _bare_metal_start_service(cfg, service, host, port)
        return

    # Full stack startup
    from skyops._client import resolve_host_for_url

    pdir = _pid_directory(cfg)
    server_url = resolve_server_url(cfg)
    agp_env = _agp_process_env(cfg)

    # Derive hosts from config URLs (not hardcoded)
    infra_checks = [
        ("postgres", resolve_host_for_url(cfg.database.url), _parse_port_from_url(cfg.database.url) or 5432),
        ("redis", resolve_host_for_url(cfg.redis.url), _parse_port_from_url(cfg.redis.url) or 6379),
        ("minio", resolve_host_for_url(cfg.s3.endpoint_url), _parse_port_from_url(cfg.s3.endpoint_url) or 9000),
    ]

    # Wait for infrastructure deps (assumed externally managed in bare-metal)
    typer.echo("Checking infrastructure dependencies...")
    for label, h, p in infra_checks:
        if _probe_tcp(h, p):
            typer.echo(f"  {label} already running on {h}:{p}")
        else:
            typer.echo(f"  WARNING: {label} not reachable on {h}:{p}")

    first_boot = _is_first_boot(cfg)

    # Init DB (only on first boot)
    if first_boot:
        typer.echo("First boot detected — initializing database...")
        subprocess.run(["agp", "initdb"], check=True, env=agp_env)

    # Start control plane
    _start_bg(["agp", "serve", "--host", host, "--port", str(port)], "control-plane", pdir, env=agp_env)
    _wait_http(f"{server_url}/health", "control-plane")

    # Seed on first boot
    if first_boot:
        typer.echo("Seeding data from skyops.toml...")
        from skyops._db import db_seed
        try:
            db_seed()
        except Exception as e:
            typer.echo(f"  Seeding warning: {e}")
        _mark_initialized(cfg)

    # Start sweepers
    _start_bg(["agp", "sweep-loop", "--interval-seconds", "5"], "lease-sweeper", pdir, env=agp_env)
    _start_bg(["agp", "sweep-runtimes-loop", "--interval-seconds", "10"], "runtime-sweeper", pdir, env=agp_env)

    # Start runtime work loop if agents are configured
    if cfg.agents:
        agent_id = next(iter(cfg.agents))
        runtime_id = "rtm_local"
        _start_bg(
            ["agp", "runtime-work-loop", runtime_id,
             "--server-url", server_url,
             "--agent-id", agent_id,
             "--host-kind", cfg.runtime.host_kind,
             "--adapter-kind", cfg.runtime.adapter_kind],
            "runtime", pdir, env=agp_env,
        )

    typer.echo("Stack is up.")


def _bare_metal_down(cfg: SkyopsConfig, service: str | None) -> None:
    """Stop bare-metal services by signalling PID-tracked processes."""
    pdir = _pid_directory(cfg)
    if service:
        if signal_and_wait(pdir, service):
            typer.echo(f"Stopped {service}.")
        else:
            typer.echo(f"{service} was not running.")
    else:
        services = list_pidfiles(pdir)
        if not services:
            typer.echo("No tracked services running.")
            return
        for label in services:
            signal_and_wait(pdir, label)
        typer.echo("Stopped.")


# ── Profile generation ─────────────────────────────────────────────


def _write_profile(cfg: SkyopsConfig) -> None:
    """Write ~/.agp/profiles/default.toml from skyops config."""
    profiles_dir = Path.home() / ".agp" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profiles_dir / "default.toml"
    host = _connectable_host(cfg.server.host)
    existing_token = None
    if profile_path.exists():
        try:
            with open(profile_path, "rb") as f:
                data = tomllib.load(f)
            existing_token = data.get("token")
        except Exception:
            existing_token = None
    token = cfg.security.operator_token or existing_token
    token_line = f'token = "{token}"\n' if token else ""
    profile_path.write_text(
        f'# Generated by skyops up\n'
        f'server_url = "http://{host}:{cfg.server.port}"\n'
        f'{token_line}'
    )
    typer.echo(f"Profile written to {profile_path}")


def _parse_port_from_url(url: str) -> int | None:
    from urllib.parse import urlparse
    return urlparse(url).port


# ── CLI commands ───────────────────────────────────────────────────


@lifecycle_app.command("up")
def up(
    service: str | None = typer.Argument(None, help="Start a single service instead of the full stack."),
    build: bool = typer.Option(True, "--build/--no-build", help="Rebuild images (docker mode)."),
) -> None:
    """Start the AGP stack."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_up(cfg, service, build)
        # First-boot: the compose bootstrap service handles db init + seed,
        # but mark initialized so bare-metal mode knows too
        if not service and _is_first_boot(cfg):
            _mark_initialized(cfg)
    else:
        _bare_metal_up(cfg, service)

    if not service:
        _write_profile(cfg)


@lifecycle_app.command("ps")
def ps() -> None:
    """Show detailed process list with resource usage."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        cmd = _compose_cmd(cfg) + ["ps", "-a"]
        try:
            subprocess.run(cmd, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            typer.echo(f"Docker unavailable: {exc}", err=True)
            raise typer.Exit(1)
    else:
        pdir = _pid_directory(cfg)
        services = list_pidfiles(pdir)
        if not services:
            typer.echo("No tracked bare-metal services running.")
            return
        typer.echo(f"{'SERVICE':<25} {'PID':<10} {'STATUS'}")
        for label, p in sorted(services.items()):
            typer.echo(f"{label:<25} {p:<10} {'alive'}")


@lifecycle_app.command("down")
def down(
    service: str | None = typer.Argument(None, help="Stop a single service instead of the full stack."),
    volumes: bool = typer.Option(False, "-v", "--volumes", help="Remove volumes (docker mode)."),
) -> None:
    """Stop the AGP stack."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_down(cfg, service, volumes)
    else:
        _bare_metal_down(cfg, service)


@lifecycle_app.command("restart")
def restart(
    service: str | None = typer.Argument(None, help="Restart a single service."),
    build: bool = typer.Option(True, "--build/--no-build", help="Rebuild images (docker mode)."),
) -> None:
    """Restart the AGP stack (down then up)."""
    cfg = load_config()
    if cfg.stack.mode == "docker":
        _docker_down(cfg, service, volumes=False)
        _docker_up(cfg, service, build)
    else:
        _bare_metal_down(cfg, service)
        _bare_metal_up(cfg, service)

    if not service:
        _write_profile(cfg)
