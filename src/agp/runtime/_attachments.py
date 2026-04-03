"""Helpers for staging job attachments into a workspace."""

from __future__ import annotations

from pathlib import Path

# Canonical temp directory for AGP-managed workspace artifacts.
AGP_TMP_DIR = Path(".agp-tmp")
AGP_ATTACHMENTS_DIR = AGP_TMP_DIR / "attachments"


def staged_attachment_relative_path(*, artifact_id: str, name: str) -> Path:
    safe_parts = [part for part in Path(name).parts if part not in ("", ".", "..")]
    if not safe_parts:
        safe_parts = ["attachment.txt"]
    return AGP_ATTACHMENTS_DIR / artifact_id / Path(*safe_parts)


def cleanup_temp_artifacts(workspace: Path | None = None) -> int:
    """Remove AGP temp artifacts from a workspace directory.

    Cleans both the new ``.agp-tmp/`` and legacy ``agp-attachments/`` dirs.
    Returns the number of directories cleaned.
    """
    import shutil
    cleaned = 0
    root = workspace or Path.cwd()
    for dirname in (AGP_TMP_DIR, Path("agp-attachments")):
        target = root / dirname
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            cleaned += 1
    return cleaned


def cleanup_stale_result_files(*, max_age_seconds: float = 3600) -> int:
    """Remove stale /tmp/agp-results-* files older than *max_age_seconds*.

    Only deletes files that are clearly from previous runs (default: >1 hour old)
    to avoid destroying result files for in-flight runs owned by the same user.
    Returns the number of files cleaned.
    """
    import os
    import time
    private_dir = Path(f"/tmp/agp-results-{os.getuid()}")
    cleaned = 0
    now = time.time()
    if private_dir.is_dir():
        for f in private_dir.iterdir():
            try:
                age = now - f.stat().st_mtime
                if age > max_age_seconds:
                    f.unlink()
                    cleaned += 1
            except Exception:
                pass
    return cleaned
