# DEPRECATED: Use `skyops backup create` instead.
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime, UTC
from pathlib import Path

import boto3


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


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AGP_S3_ENDPOINT_URL", "http://127.0.0.1:9000"),
        aws_access_key_id=os.environ.get("AGP_S3_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AGP_S3_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.environ.get("AGP_S3_REGION", "us-east-1"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a Phase 3 backup from PostgreSQL and MinIO.")
    parser.add_argument("backup_dir", help="Directory where the backup snapshot will be created.")
    args = parser.parse_args()

    backup_dir = Path(args.backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    objects_dir = backup_dir / "s3-objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    sql_path = backup_dir / "postgres.sql"

    docker = _docker_cmd()
    dump = _run(*docker, "compose", "-f", COMPOSE_FILE, "exec", "-T", "postgres", "pg_dump", "-U", "agp", "-d", "agp")
    sql_path.write_bytes(dump.stdout)

    bucket = os.environ.get("AGP_S3_BUCKET", "agp-artifacts")
    client = _s3_client()
    paginator = client.get_paginator("list_objects_v2")
    object_count = 0
    for page in paginator.paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = item["Key"]
            target = objects_dir / key
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            object_count += 1

    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "compose_file": str(COMPOSE_FILE),
        "postgres_dump": str(sql_path),
        "s3_snapshot_dir": str(objects_dir),
        "s3_bucket": bucket,
        "s3_endpoint_url": os.environ.get("AGP_S3_ENDPOINT_URL", "http://127.0.0.1:9000"),
        "object_count": object_count,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"ok": True, "backup_dir": str(backup_dir), "object_count": object_count}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
