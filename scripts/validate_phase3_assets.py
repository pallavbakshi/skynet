# DEPRECATED: Use `skyops backup validate` instead.
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str) -> dict:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return {
        "cmd": list(args),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _docker_cmd() -> list[str] | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    direct = subprocess.run([docker, "info"], cwd=ROOT, capture_output=True, text=True)
    if direct.returncode == 0:
        return [docker]
    sudo = shutil.which("sudo")
    if sudo:
        elevated = subprocess.run([sudo, "-n", docker, "info"], cwd=ROOT, capture_output=True, text=True)
        if elevated.returncode == 0:
            return [sudo, docker]
    return [docker]


def main() -> int:
    checks: dict[str, dict] = {}

    docker = _docker_cmd()
    if docker:
        checks["compose_phase3"] = _run(*docker, "compose", "-f", "compose.phase3.yaml", "config")
    else:
        checks["compose_phase3"] = {"skipped": True, "reason": "docker not installed"}

    kubectl = shutil.which("kubectl")
    if kubectl:
        checks["k8s_kustomize"] = _run(kubectl, "kustomize", "k8s")
        checks["k8s_kind_overlay_kustomize"] = _run(
            kubectl, "kustomize", "k8s/overlays/kind", "--load-restrictor=LoadRestrictionsNone"
        )
    else:
        checks["k8s_kustomize"] = {"skipped": True, "reason": "kubectl not installed"}
        checks["k8s_kind_overlay_kustomize"] = {"skipped": True, "reason": "kubectl not installed"}

    ok = True
    for item in checks.values():
        if item.get("skipped"):
            continue
        if item.get("returncode") != 0:
            ok = False

    print(json.dumps({"ok": ok, "checks": checks}, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
