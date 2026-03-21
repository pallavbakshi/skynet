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

    # Check bucket is not public — must get 403/401 to pass
    from urllib.request import urlopen
    from urllib.error import HTTPError, URLError

    bucket_url = f"{cfg.s3.endpoint_url}/{cfg.s3.bucket}/"
    try:
        urlopen(bucket_url, timeout=5)
        typer.echo(f"FAIL: bucket {cfg.s3.bucket} is publicly accessible.", err=True)
        raise typer.Exit(1)
    except HTTPError as e:
        if e.code not in (403, 401):
            typer.echo(f"FAIL: unexpected HTTP {e.code} checking bucket policy.", err=True)
            raise typer.Exit(1)
    except URLError as e:
        typer.echo(f"FAIL: S3 endpoint unreachable at {bucket_url} — cannot verify bucket policy: {e}", err=True)
        raise typer.Exit(1)

    typer.echo("Smoke test passed.")


@validate_app.command("k8s-smoke")
def k8s_smoke(
    cluster_name: str = typer.Option("agp-phase3", "--cluster", help="Kind cluster name."),
    image: str = typer.Option("agp:latest", "--image", help="Docker image to build and load."),
    overlay: str = typer.Option("k8s/overlays/kind", "--overlay", help="Kustomize overlay path."),
    timeout: int = typer.Option(180, "--timeout", help="Timeout in seconds for each wait step."),
    skip_build: bool = typer.Option(False, "--skip-build", help="Skip image build."),
    skip_load: bool = typer.Option(False, "--skip-load", help="Skip image load into kind."),
) -> None:
    """Full kind cluster lifecycle + smoke test.

    Creates a kind cluster, builds the image, deploys, waits for bootstrap,
    runs a smoke job, and validates it succeeds.
    """
    kctl = ["kubectl"]

    # ── Cluster setup ─────────────────────────────────────────────
    typer.echo(f"[1/7] Creating kind cluster: {cluster_name}...")
    subprocess.run(["kind", "delete", "cluster", "--name", cluster_name], capture_output=True)
    subprocess.run(["kind", "create", "cluster", "--name", cluster_name], check=True)

    if not skip_build:
        typer.echo(f"[2/7] Building image: {image}...")
        subprocess.run(["docker", "build", "-t", image, "."], check=True)
    else:
        typer.echo("[2/7] Skipping image build.")

    if not skip_load:
        typer.echo("[3/7] Loading image into kind cluster...")
        subprocess.run(["kind", "load", "docker-image", image, "--name", cluster_name], check=True)
    else:
        typer.echo("[3/7] Skipping image load.")

    # ── Apply manifests ───────────────────────────────────────────
    typer.echo("[4/7] Applying k8s manifests...")
    subprocess.run(kctl + ["delete", "namespace", "agp", "--ignore-not-found", "--wait=true"], capture_output=True)
    rendered = subprocess.run(
        kctl + ["kustomize", overlay, "--load-restrictor=LoadRestrictionsNone"],
        capture_output=True, text=True, check=True,
    )
    subprocess.run(kctl + ["apply", "-f", "-"], input=rendered.stdout, text=True, check=True)

    # ── Wait for core services ────────────────────────────────────
    typer.echo("[5/7] Waiting for core services...")
    for deploy in ["postgres", "minio", "redis", "control-plane"]:
        subprocess.run(
            kctl + ["wait", "--namespace", "agp", "--for=condition=available",
                    f"deployment/{deploy}", f"--timeout={timeout}s"],
            check=True,
        )

    # ── Wait for bootstrap job ────────────────────────────────────
    typer.echo("[6/7] Waiting for bootstrap job...")
    import time as _time
    deadline = _time.monotonic() + timeout
    bootstrap_ok = False
    while _time.monotonic() < deadline:
        result = subprocess.run(
            kctl + ["get", "job", "agp-bootstrap", "--namespace", "agp",
                    "-o", "jsonpath={.status.succeeded}"],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "1":
            bootstrap_ok = True
            break
        _time.sleep(2)
    if not bootstrap_ok:
        subprocess.run(kctl + ["logs", "job/agp-bootstrap", "--namespace", "agp", "--tail=50"])
        typer.echo("Bootstrap job did not succeed.", err=True)
        raise typer.Exit(1)

    for deploy in ["lease-sweeper", "runtime-sweeper", "runtime"]:
        subprocess.run(
            kctl + ["wait", "--namespace", "agp", "--for=condition=available",
                    f"deployment/{deploy}", f"--timeout={timeout}s"],
            check=True,
        )

    # ── Run smoke job ─────────────────────────────────────────────
    typer.echo("[7/7] Running smoke job...")
    smoke_manifest = """\
apiVersion: batch/v1
kind: Job
metadata:
  name: agp-smoke
  namespace: agp
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app: agp-smoke
    spec:
      restartPolicy: Never
      containers:
        - name: smoke
          image: {image}
          imagePullPolicy: Never
          command: ["python", "/app/scripts/smoke_local_stack.py"]
          envFrom:
            - configMapRef:
                name: agp-config
            - secretRef:
                name: agp-secrets
""".format(image=image)
    subprocess.run(kctl + ["delete", "job", "agp-smoke", "--namespace", "agp", "--ignore-not-found", "--wait=true"], capture_output=True)
    subprocess.run(kctl + ["apply", "-f", "-"], input=smoke_manifest, text=True, check=True)

    deadline = _time.monotonic() + timeout
    smoke_ok = False
    while _time.monotonic() < deadline:
        result = subprocess.run(
            kctl + ["get", "job", "agp-smoke", "--namespace", "agp",
                    "-o", "jsonpath={.status.succeeded}"],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "1":
            smoke_ok = True
            break
        # Check for failure
        fail_result = subprocess.run(
            kctl + ["get", "job", "agp-smoke", "--namespace", "agp",
                    "-o", "jsonpath={.status.failed}"],
            capture_output=True, text=True,
        )
        if fail_result.stdout.strip() not in ("", "0"):
            subprocess.run(kctl + ["logs", "job/agp-smoke", "--namespace", "agp", "--tail=50"])
            typer.echo("Smoke job failed.", err=True)
            raise typer.Exit(1)
        _time.sleep(2)

    if not smoke_ok:
        subprocess.run(kctl + ["logs", "job/agp-smoke", "--namespace", "agp", "--tail=50"])
        typer.echo("Smoke job did not succeed before timeout.", err=True)
        raise typer.Exit(1)

    typer.echo("")
    typer.echo("Kubernetes smoke passed.")
    typer.echo(f"Cluster: {cluster_name}")
    typer.echo(f"Clean up: kind delete cluster --name {cluster_name}")
