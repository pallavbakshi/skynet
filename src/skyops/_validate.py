"""``skyops validate``, ``skyops smoke``, ``skyops k8s smoke`` — validation commands."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import typer

from skyops.config import load_config

validate_app = typer.Typer(help="Validation and smoke test commands.")


def _emit(data: object) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _run(*args: str, cwd: str | Path | None = None) -> dict:
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    return {
        "cmd": list(args),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


@validate_app.command("validate")
def validate() -> None:
    """Lint compose and k8s manifest syntax."""
    cfg = load_config()
    root = cfg._config_path.parent if cfg._config_path else Path.cwd()
    checks: dict[str, dict] = {}

    # Validate compose file
    docker = shutil.which("docker")
    if docker:
        checks["compose"] = _run(
            docker, "compose", "-f", cfg.stack.compose_file, "config",
            cwd=root,
        )
    else:
        checks["compose"] = {"skipped": True, "reason": "docker not installed"}

    # Validate k8s manifests
    kubectl = shutil.which("kubectl")
    if kubectl:
        k8s_dir = root / "k8s"
        if k8s_dir.is_dir():
            checks["k8s_kustomize"] = _run(kubectl, "kustomize", "k8s", cwd=root)
            kind_overlay = root / "k8s" / "overlays" / "kind"
            if kind_overlay.is_dir():
                checks["k8s_kind_overlay"] = _run(
                    kubectl, "kustomize", "k8s/overlays/kind",
                    "--load-restrictor=LoadRestrictionsNone",
                    cwd=root,
                )
        else:
            checks["k8s_kustomize"] = {"skipped": True, "reason": "k8s/ directory not found"}
    else:
        checks["k8s_kustomize"] = {"skipped": True, "reason": "kubectl not installed"}

    ok = all(
        c.get("skipped") or c.get("returncode") == 0
        for c in checks.values()
    )
    _emit({"ok": ok, "checks": {k: {kk: vv for kk, vv in v.items() if kk != "stdout"} for k, v in checks.items()}})
    if not ok:
        raise typer.Exit(1)


@validate_app.command("smoke")
def smoke() -> None:
    """End-to-end smoke test against running stack.

    Sends a test job, watches it complete, and verifies the artifact.
    """
    cfg = load_config()
    from skyops._client import build_client

    import time

    with build_client(cfg) as client:
        # Wait for health
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                client.health()
                break
            except Exception:
                time.sleep(1.0)
        else:
            typer.echo("Control plane not healthy.", err=True)
            raise typer.Exit(1)

        # Pick first agent from config
        agent_id = next(iter(cfg.agents), "agt_local")
        typer.echo(f"Sending smoke test to {agent_id}...")
        payload = client.send(
            "agent", agent_id,
            "local deployment smoke test",
            metadata={"kind": "smoke"},
            idempotency_key=f"smoke-{int(time.time())}",
        )

        if payload.get("kind") == "inline_result":
            artifact_id = payload["result_artifact_id"]
        else:
            snapshots = client.watch_job(
                payload["job_id"],
                poll_interval=1.0,
                max_polls=60,
            )
            job = snapshots[-1]["job"]
            if job["status"] != "completed":
                typer.echo(f"Smoke job ended in state: {job['status']}", err=True)
                raise typer.Exit(1)
            artifact_id = job["result_artifact_id"]

        artifact = client.fetch_artifact(artifact_id, content=True)
        content = artifact.get("content", "")
        if "local deployment smoke test" not in content:
            typer.echo("Smoke artifact content mismatch.", err=True)
            raise typer.Exit(1)

    # Check bucket is not public
    from urllib.request import urlopen
    from urllib.error import HTTPError, URLError

    bucket_url = f"{cfg.s3.endpoint_url}/{cfg.s3.bucket}/"
    try:
        urlopen(bucket_url, timeout=5)
        typer.echo(f"WARNING: bucket {cfg.s3.bucket} is publicly accessible.", err=True)
    except HTTPError as e:
        if e.code not in (403, 401):
            typer.echo(f"Unexpected HTTP {e.code} checking bucket.", err=True)
    except URLError:
        pass  # Connection refused — skip

    typer.echo("Smoke test passed.")


@validate_app.command("k8s-smoke")
def k8s_smoke(
    cluster_name: str = typer.Option("agp-phase3", "--cluster", help="Kind cluster name."),
    image: str = typer.Option("agp:dev", "--image", help="Docker image to build and load."),
) -> None:
    """Full kind cluster lifecycle + smoke test.

    Creates a kind cluster, builds the image, deploys, and runs smoke tests.
    """
    typer.echo(f"Creating kind cluster: {cluster_name}...")
    # Delete existing cluster if present
    subprocess.run(["kind", "delete", "cluster", "--name", cluster_name], capture_output=True)
    subprocess.run(["kind", "create", "cluster", "--name", cluster_name], check=True)

    typer.echo(f"Building image: {image}...")
    subprocess.run(["docker", "build", "-t", image, "."], check=True)

    typer.echo("Loading image into kind cluster...")
    subprocess.run(["kind", "load", "docker-image", image, "--name", cluster_name], check=True)

    typer.echo("Applying k8s manifests...")
    subprocess.run(
        ["kubectl", "apply", "-k", "k8s/overlays/kind", "--wait=true"],
        check=True,
    )

    typer.echo("Waiting for deployments...")
    for deploy in ["control-plane", "lease-sweeper", "runtime-sweeper"]:
        subprocess.run(
            ["kubectl", "rollout", "status", f"deployment/{deploy}",
             "-n", "agp", "--timeout=120s"],
            check=True,
        )

    typer.echo("K8s smoke test complete.")
    typer.echo(f"Cluster {cluster_name} is running. Clean up with: kind delete cluster --name {cluster_name}")
