from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import boto3
import httpx


ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "compose.phase3.yaml"


def _docker_cmd() -> list[str]:
    docker = shutil.which("docker")
    if docker and subprocess.run([docker, "info"], cwd=ROOT, capture_output=True).returncode == 0:
        return [docker]
    sudo = shutil.which("sudo")
    if docker and sudo and subprocess.run([sudo, "-n", docker, "info"], cwd=ROOT, capture_output=True).returncode == 0:
        return [sudo, docker]
    raise RuntimeError("docker daemon is not reachable")


def _run(*args: str | Path, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=ROOT,
        input=input_bytes,
        capture_output=True,
        check=True,
    )


def _s3_client(endpoint_url: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=os.environ.get("AGP_S3_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AGP_S3_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.environ.get("AGP_S3_REGION", "us-east-1"),
    )


def _wait_for_postgres(docker: list[str]) -> None:
    deadline = time.time() + 120
    while time.time() < deadline:
        result = subprocess.run(
            [*docker, "compose", "-f", str(COMPOSE_FILE), "exec", "-T", "postgres", "pg_isready", "-U", "agp", "-d", "agp"],
            cwd=ROOT,
            capture_output=True,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError("postgres did not become ready")


def _wait_for_minio(endpoint_url: str) -> None:
    deadline = time.time() + 120
    with httpx.Client(timeout=5.0) as client:
        while time.time() < deadline:
            try:
                response = client.get(f"{endpoint_url}/minio/health/live")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)
    raise RuntimeError("minio did not become ready")


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a Phase 3 backup into PostgreSQL and MinIO.")
    parser.add_argument("backup_dir", help="Backup directory created by phase3_backup_create.py")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir).resolve()
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"missing backup manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sql_path = Path(manifest["postgres_dump"])
    objects_dir = Path(manifest["s3_snapshot_dir"])
    bucket = manifest["s3_bucket"]
    endpoint_url = manifest["s3_endpoint_url"]

    _run(ROOT / "scripts" / "phase3_stack_down.sh")

    docker = _docker_cmd()
    _run(*docker, "compose", "-f", COMPOSE_FILE, "up", "-d", "postgres", "minio", "redis")
    _wait_for_postgres(docker)
    _wait_for_minio(endpoint_url)

    _run(
        *docker, "compose", "-f", COMPOSE_FILE, "exec", "-T", "postgres",
        "psql", "-U", "agp", "-d", "postgres", "-c", "DROP DATABASE IF EXISTS agp WITH (FORCE);",
    )
    _run(
        *docker, "compose", "-f", COMPOSE_FILE, "exec", "-T", "postgres",
        "psql", "-U", "agp", "-d", "postgres", "-c", "CREATE DATABASE agp OWNER agp;",
    )
    _run(
        *docker, "compose", "-f", COMPOSE_FILE, "exec", "-T", "postgres",
        "psql", "-U", "agp", "-d", "agp",
        input_bytes=sql_path.read_bytes(),
    )

    client = _s3_client(endpoint_url)
    try:
        client.head_bucket(Bucket=bucket)
    except Exception:
        if os.environ.get("AGP_S3_REGION", "us-east-1") == "us-east-1":
            client.create_bucket(Bucket=bucket)
        else:
            client.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": os.environ.get("AGP_S3_REGION", "us-east-1")},
            )

    existing = client.list_objects_v2(Bucket=bucket)
    if existing.get("Contents"):
        client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": item["Key"]} for item in existing["Contents"]], "Quiet": True},
        )

    restored_objects = 0
    for path in objects_dir.rglob("*"):
        if not path.is_file():
            continue
        key = path.relative_to(objects_dir).as_posix()
        client.upload_file(str(path), bucket, key)
        restored_objects += 1

    runner = [ROOT / "scripts" / "phase3_stack_up.sh"]
    completed = subprocess.run([str(item) for item in runner], cwd=ROOT)
    if completed.returncode != 0:
        return completed.returncode

    print(json.dumps({"ok": True, "restored_objects": restored_objects, "backup_dir": str(backup_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
