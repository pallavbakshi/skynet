"""Structured log rotation, reading, and retention helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _rotated_log_path(path: Path, *, now: datetime | None = None) -> Path:
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")
    return path.with_name(f"{path.stem}.{timestamp}{path.suffix}")


def append_jsonl_log(path: Path, entry: dict[str, Any], *, rotation_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if rotation_bytes > 0 and path.exists() and path.stat().st_size >= rotation_bytes:
        path.rename(_rotated_log_path(path))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, default=str, sort_keys=True) + "\n")


def read_tail_jsonl_family(path: Path, *, limit: int) -> list[dict]:
    if limit <= 0:
        return []
    rotated = sorted(path.parent.glob(f"{path.stem}.*{path.suffix}"))
    ordered_paths = [*rotated]
    if path.exists():
        ordered_paths.append(path)
    lines: list[str] = []
    for candidate in ordered_paths:
        if not candidate.exists():
            continue
        lines.extend(candidate.read_text(encoding="utf-8").splitlines())
    result: list[dict] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        result.append(json.loads(line))
    return result


def prune_rotated_jsonl_family(
    path: Path,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> dict[str, int]:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    deleted = 0
    kept = 0
    for candidate in path.parent.glob(f"{path.stem}.*{path.suffix}"):
        mtime = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if mtime < cutoff:
            candidate.unlink(missing_ok=True)
            deleted += 1
        else:
            kept += 1
    return {"deleted": deleted, "kept": kept}
