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


def main() -> int:
    checks: dict[str, dict] = {}

    docker = shutil.which("docker")
    if docker:
        checks["compose_phase3"] = _run(docker, "compose", "-f", "compose.phase3.yaml", "config")
    else:
        checks["compose_phase3"] = {"skipped": True, "reason": "docker not installed"}

    kubectl = shutil.which("kubectl")
    if kubectl:
        checks["k8s_kustomize"] = _run(kubectl, "kustomize", "k8s")
    else:
        checks["k8s_kustomize"] = {"skipped": True, "reason": "kubectl not installed"}

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
