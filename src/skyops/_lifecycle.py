"""``skyops up``, ``skyops down``, ``skyops restart`` — service lifecycle."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import typer

from skyops.config import SkyopsConfig, load_config
from skyops._status import _probe_tcp, _probe_http_health

lifecycle_app = typer.Typer(help="Manage AGP stack services.")

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

_BG_PIDS: list[subprocess.Popen] = []


def _start_bg(cmd: list[str], label: str) -> subprocess.Popen:
    typer.echo(f"Starting {label}: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _BG_PIDS.append(proc)
    return proc


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


def _bare_metal_up(cfg: SkyopsConfig, service: str | None) -> None:
    """Start bare-metal services. If *service* is given, start only that one."""
    port = cfg.server.port
    host = cfg.server.host

    if service:
        # Single service start
        if service == "control-plane":
            _start_bg(["agp", "serve", "--host", host, "--port", str(port)], "control-plane")
            _wait_http(f"http://127.0.0.1:{port}/health", "control-plane")
        elif service == "sweep-loop":
            _start_bg(["agp", "sweep-loop", "--interval-seconds", "5"], "lease-sweeper")
        elif service == "sweep-runtimes-loop":
            _start_bg(["agp", "sweep-runtimes-loop", "--interval-seconds", "10"], "runtime-sweeper")
        else:
            typer.echo(f"Unknown service: {service}", err=True)
            raise typer.Exit(1)
        return

    # Full stack startup
    db_port = 5432
    redis_port = 6379
    minio_port = 9000

    # Wait for infrastructure deps (assumed externally managed in bare-metal)
    typer.echo("Checking infrastructure dependencies...")
    for label, p in [("postgres", db_port), ("redis", redis_port), ("minio", minio_port)]:
        if _probe_tcp("127.0.0.1", p):
            typer.echo(f"  {label} already running on :{p}")
        else:
            typer.echo(f"  WARNING: {label} not reachable on :{p}")

    first_boot = _is_first_boot(cfg)

    # Init DB (only on first boot)
    if first_boot:
        typer.echo("First boot detected — initializing database...")
        subprocess.run(["agp", "initdb"], check=True)

    # Start control plane
    _start_bg(["agp", "serve", "--host", host, "--port", str(port)], "control-plane")
    _wait_http(f"http://127.0.0.1:{port}/health", "control-plane")

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
    _start_bg(["agp", "sweep-loop", "--interval-seconds", "5"], "lease-sweeper")
    _start_bg(["agp", "sweep-runtimes-loop", "--interval-seconds", "10"], "runtime-sweeper")

    # Start runtime work loop if agents are configured
    if cfg.agents:
        agent_id = next(iter(cfg.agents))
        runtime_id = "rtm_local"
        _start_bg(
            ["agp", "runtime-work-loop", runtime_id,
             "--server-url", f"http://127.0.0.1:{port}",
             "--agent-id", agent_id,
             "--host-kind", cfg.runtime.host_kind,
             "--adapter-kind", cfg.runtime.adapter_kind],
            "runtime",
        )

    typer.echo("Stack is up.")


def _bare_metal_down(service: str | None) -> None:
    """Stop bare-metal services by signalling background processes."""
    # In bare-metal mode we rely on finding agp processes
    # For processes we started, signal them directly
    for proc in _BG_PIDS:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    _BG_PIDS.clear()

    # Also try to find and kill agp processes via pkill
    if not service:
        for pattern in ["agp serve", "agp sweep-loop", "agp sweep-runtimes-loop"]:
            subprocess.run(["pkill", "-f", pattern], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", f"agp {service}"], capture_output=True)
    typer.echo("Stopped." if not service else f"Stopped {service}.")


# ── Profile generation ─────────────────────────────────────────────


def _write_profile(cfg: SkyopsConfig) -> None:
    """Write ~/.agp/profiles/default.toml from skyops config."""
    profiles_dir = Path.home() / ".agp" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    profile_path = profiles_dir / "default.toml"
    token_line = f'token = "{cfg.security.operator_token}"\n' if cfg.security.operator_token else ""
    profile_path.write_text(
        f'# Generated by skyops up\n'
        f'server_url = "http://127.0.0.1:{cfg.server.port}"\n'
        f'{token_line}'
    )
    typer.echo(f"Profile written to {profile_path}")


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
        subprocess.run(cmd)
    else:
        typer.echo("AGP processes:")
        subprocess.run("ps aux | head -1; ps aux | grep '[a]gp'", shell=True)


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
        _bare_metal_down(service)


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
        _bare_metal_down(service)
        _bare_metal_up(cfg, service)

    if not service:
        _write_profile(cfg)
