"""Backup, restore, and recovery operations."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import select

from agp._local_state import ensure_local_control_plane_stopped
from agp.config import settings
from agp.artifact_store import get_artifact_store
from agp.db import SessionLocal, engine
from agp.models import Artifact, HandoffArtifact, JobArtifact, RunArtifact
from agp._ops_helpers._maintenance import reconstruct_queue_from_state


def _sqlite_db_path() -> Path:
    prefix = "sqlite+pysqlite:///"
    if not settings.database_url.startswith(prefix):
        raise ValueError("backup/restore currently supports sqlite+pysqlite URLs only")
    return Path(settings.database_url.removeprefix(prefix))


def create_backup_snapshot(*, backup_dir: str | Path) -> dict:
    backup_path = Path(backup_dir)
    backup_path.mkdir(parents=True, exist_ok=True)

    db_path = _sqlite_db_path()
    db_backup_path = backup_path / "agp.db"
    artifact_backup_path = backup_path / "artifacts"

    engine.dispose()
    if db_path.exists():
        with sqlite3.connect(db_path) as source_conn:
            with sqlite3.connect(db_backup_path) as backup_conn:
                source_conn.backup(backup_conn)

    if artifact_backup_path.exists():
        shutil.rmtree(artifact_backup_path)
    if settings.artifact_root.exists():
        shutil.copytree(settings.artifact_root, artifact_backup_path)
    else:
        artifact_backup_path.mkdir(parents=True, exist_ok=True)

    manifest = {
        "database_url": settings.database_url,
        "artifact_backend": settings.artifact_backend,
        "artifact_root": str(settings.artifact_root),
        "db_snapshot": str(db_backup_path),
        "artifact_snapshot": str(artifact_backup_path),
    }
    (backup_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def restore_backup_snapshot(
    *,
    backup_dir: str | Path,
    require_stopped_local_cp: bool = True,
) -> dict:
    backup_path = Path(backup_dir)
    manifest_path = backup_path / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"missing backup manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    db_path = _sqlite_db_path()
    db_backup_path = Path(manifest["db_snapshot"])
    artifact_backup_path = Path(manifest["artifact_snapshot"])

    if require_stopped_local_cp:
        ensure_local_control_plane_stopped(root=Path.cwd())
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()
    if db_backup_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db_backup_path, db_path)
    else:
        from agp.migrations import apply_migrations
        apply_migrations()  # schema only; restore will repopulate data

    if settings.artifact_root.exists():
        shutil.rmtree(settings.artifact_root)
    if artifact_backup_path.exists():
        shutil.copytree(artifact_backup_path, settings.artifact_root)
    else:
        settings.artifact_root.mkdir(parents=True, exist_ok=True)

    return {
        "database_url": settings.database_url,
        "artifact_backend": settings.artifact_backend,
        "restored_from": str(backup_path),
    }


def restore_and_recover_snapshot(
    *,
    backup_dir: str | Path,
    validate_limit: int | None = None,
    require_stopped_local_cp: bool = True,
) -> dict:
    restored = restore_backup_snapshot(
        backup_dir=backup_dir,
        require_stopped_local_cp=require_stopped_local_cp,
    )
    validation = validate_restored_state(limit=validate_limit)
    reconstructed = reconstruct_queue_from_state()
    return {
        "restored": restored,
        "validation": validation,
        "queue_reconstruction": reconstructed,
        "ok": validation["ok"],
    }


def _safe_exists(store, ref: str) -> bool:
    """Call store.exists, returning False on transient errors."""
    try:
        return store.exists(storage_ref=ref)
    except Exception:
        return False


def validate_restored_state(*, limit: int | None = None) -> dict:
    artifact_store = get_artifact_store(settings.artifact_backend, settings.artifact_root)
    session = SessionLocal()
    try:
        query = select(Artifact).order_by(Artifact.created_at.asc())
        if limit is not None:
            query = query.limit(limit)
        artifacts = session.scalars(query).all()
        missing = [
            {
                "artifact_id": artifact.artifact_id,
                "storage_ref": artifact.storage_ref,
                "job_id": artifact.job_id,
                "run_id": artifact.run_id,
                "kind": artifact.kind,
            }
            for artifact in artifacts
            if not _safe_exists(artifact_store, artifact.storage_ref)
        ]
        return {
            "checked_artifacts": len(artifacts),
            "missing_artifacts": len(missing),
            "ok": len(missing) == 0,
            "missing": missing,
        }
    finally:
        session.close()
