"""``skyops deps`` — dependency checking and installation."""

from __future__ import annotations

import platform
import shutil
import subprocess

import typer

deps_app = typer.Typer(help="Check and install infrastructure dependencies.")

_DEPS = [
    ("docker", "docker"),
    ("docker-compose", "docker compose version"),
    ("psql", "psql"),
    ("redis-cli", "redis-cli"),
    ("minio", "minio"),
    ("mc", "mc"),
    ("kind", "kind"),
    ("kubectl", "kubectl"),
    ("python3", "python3"),
]


def _check_dep(name: str) -> tuple[str, bool, str]:
    """Check if a dependency is available. Returns (name, available, version_or_error)."""
    binary = name
    if binary == "docker-compose":
        # Check via `docker compose version`
        try:
            result = subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return (name, True, result.stdout.strip())
            return (name, False, "not available")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return (name, False, "not found")

    path = shutil.which(binary)
    if path is None:
        return (name, False, "not found")

    # Try to get version
    for flag in ["--version", "version"]:
        try:
            result = subprocess.run(
                [path, flag],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "installed"
                return (name, True, version)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return (name, True, path)


@deps_app.command("check")
def deps_check() -> None:
    """Report which dependencies are installed and reachable."""
    typer.echo(f"Platform: {platform.system()} {platform.machine()}\n")
    typer.echo(f"  {'DEPENDENCY':<20} {'STATUS':<10} {'DETAIL'}")
    typer.echo(f"  {'─' * 60}")
    all_ok = True
    for name, _ in _DEPS:
        dep_name, available, detail = _check_dep(name)
        status = "OK" if available else "MISSING"
        if not available:
            all_ok = False
        typer.echo(f"  {dep_name:<20} {status:<10} {detail}")
    typer.echo("")
    if not all_ok:
        typer.echo("Some dependencies are missing. Run `skyops deps install` to install them.")
    else:
        typer.echo("All dependencies available.")


@deps_app.command("install")
def deps_install(
    mode: str = typer.Option("docker", "--mode", "-m", help="Install for docker or bare-metal mode."),
) -> None:
    """Install missing dependencies.

    Docker mode: pulls required images.
    Bare-metal: attempts install via apt/brew.
    """
    system = platform.system().lower()

    if mode == "docker":
        typer.echo("Pulling required Docker images...")
        images = [
            "postgres:16-alpine",
            "redis:7-alpine",
            "minio/minio:RELEASE.2025-02-28T09-55-16Z",
            "prom/prometheus:v2.53.0",
            "grafana/grafana:11.1.0",
        ]
        for image in images:
            typer.echo(f"  Pulling {image}...")
            subprocess.run(["docker", "pull", image], check=True)
        typer.echo("Docker images pulled.")
        return

    # Bare-metal installation
    if system == "darwin":
        pkg_mgr = shutil.which("brew")
        if not pkg_mgr:
            typer.echo("Homebrew not found. Install from https://brew.sh", err=True)
            raise typer.Exit(1)
        packages = {
            "psql": "postgresql@16",
            "redis-cli": "redis",
            "mc": "minio/stable/mc",
            "minio": "minio/stable/minio",
            "kind": "kind",
            "kubectl": "kubernetes-cli",
        }
        for dep, pkg in packages.items():
            if shutil.which(dep) is None:
                typer.echo(f"  Installing {pkg}...")
                subprocess.run(["brew", "install", pkg], check=True)
            else:
                typer.echo(f"  {dep} already installed.")
    elif system == "linux":
        # Attempt apt-based install
        apt = shutil.which("apt-get")
        if not apt:
            typer.echo("apt-get not found. Manual installation required.", err=True)
            raise typer.Exit(1)
        packages = {
            "psql": "postgresql-client-16",
            "redis-cli": "redis-tools",
        }
        missing = [pkg for dep, pkg in packages.items() if shutil.which(dep) is None]
        if missing:
            typer.echo(f"  Installing: {', '.join(missing)}")
            subprocess.run(["sudo", "apt-get", "install", "-y"] + missing, check=True)
        typer.echo("System packages installed.")
    else:
        typer.echo(f"Unsupported platform: {system}. Manual installation required.", err=True)
        raise typer.Exit(1)

    typer.echo("Done.")
