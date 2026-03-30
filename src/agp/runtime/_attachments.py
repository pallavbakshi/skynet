"""Helpers for staging job attachments into a workspace."""

from __future__ import annotations

from pathlib import Path


def staged_attachment_relative_path(*, artifact_id: str, name: str) -> Path:
    safe_parts = [part for part in Path(name).parts if part not in ("", ".", "..")]
    if not safe_parts:
        safe_parts = ["attachment.txt"]
    return Path("agp-attachments") / artifact_id / Path(*safe_parts)
