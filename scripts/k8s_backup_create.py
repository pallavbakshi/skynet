"""Kubernetes-native backup: uploads a pg_dump file and MinIO objects to a local backup directory.

Run as the main container after an init container has produced /tmp/backup/postgres.sql via pg_dump.
The script reads the SQL file from the shared emptyDir and snapshots all S3 objects, then writes a
manifest to BACKUP_DIR (a PVC mount).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import boto3


def _s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AGP_S3_ENDPOINT_URL", "http://minio:9000"),
        aws_access_key_id=os.environ.get("AGP_S3_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AGP_S3_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.environ.get("AGP_S3_REGION", "us-east-1"),
    )


def main() -> int:
    pg_dump_src = Path(os.environ.get("PG_DUMP_PATH", "/tmp/pgdump/postgres.sql"))
    backup_root = Path(os.environ.get("BACKUP_ROOT", "/backups"))
    bucket = os.environ.get("AGP_S3_BUCKET", "agp-artifacts")
    s3_endpoint = os.environ.get("AGP_S3_ENDPOINT_URL", "http://minio:9000")

    if not pg_dump_src.exists():
        print(f"pg_dump file not found: {pg_dump_src}", file=sys.stderr)
        return 1

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    sql_dest = backup_dir / "postgres.sql"
    shutil.copy2(pg_dump_src, sql_dest)

    objects_dir = backup_dir / "s3-objects"
    objects_dir.mkdir(parents=True, exist_ok=True)

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
        "postgres_dump": str(sql_dest),
        "s3_snapshot_dir": str(objects_dir),
        "s3_bucket": bucket,
        "s3_endpoint_url": s3_endpoint,
        "object_count": object_count,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Prune old backups — keep last 7 by modification time
    backups = sorted(backup_root.iterdir(), key=lambda p: p.stat().st_mtime)
    for old in backups[:-7]:
        if old.is_dir():
            shutil.rmtree(old)

    print(json.dumps({"ok": True, "backup_dir": str(backup_dir), "object_count": object_count}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
