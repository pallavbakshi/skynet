"""Failure artifact capture for smallops live-style tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest


@dataclass(slots=True)
class ArtifactContext:
    session: Any | None = None
    response: Any | None = None
    peek_text: str | None = None
    response_raw: str | None = None


ARTIFACT_CONTEXT: pytest.StashKey[ArtifactContext] = pytest.StashKey()


def record_context(item: pytest.Item, *, session: Any | None = None, response: Any | None = None) -> None:
    ctx = item.stash.get(ARTIFACT_CONTEXT, ArtifactContext())
    if session is not None:
        ctx.session = session
    if response is not None:
        ctx.response = response
        ctx.response_raw = getattr(response, "raw", "") or ""
    item.stash[ARTIFACT_CONTEXT] = ctx


def snapshot_context(item: pytest.Item) -> None:
    ctx = item.stash.get(ARTIFACT_CONTEXT, ArtifactContext())
    if ctx.session is not None and ctx.peek_text is None:
        try:
            pane = getattr(ctx.session, "_session", None)
            if pane is not None:
                ctx.peek_text = ctx.session.mux.peek(pane)
            else:
                ctx.peek_text = "<no active smallops pane>"
        except (AttributeError, OSError, RuntimeError) as exc:
            ctx.peek_text = f"<peek failed: {exc!r}>"
    if ctx.response is not None and ctx.response_raw is None:
        ctx.response_raw = getattr(ctx.response, "raw", "") or ""
    item.stash[ARTIFACT_CONTEXT] = ctx


def dump_failure_artifacts(item: pytest.Item) -> Path | None:
    ctx = item.stash.get(ARTIFACT_CONTEXT, ArtifactContext())
    if ctx.session is None and ctx.response is None and ctx.peek_text is None and ctx.response_raw is None:
        return None

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    safe_id = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in item.nodeid)
    suffix = f"{ts}-{worker}" if worker else ts
    out_dir = Path("test-artifacts") / f"{safe_id}-{suffix}"
    out_dir.mkdir(parents=True, exist_ok=True)

    if ctx.peek_text is not None:
        (out_dir / "peek.txt").write_text(ctx.peek_text, encoding="utf-8")
    elif ctx.session is not None:
        try:
            pane = getattr(ctx.session, "_session", None)
            text = ctx.session.mux.peek(pane) if pane is not None else "<no active smallops pane>"
            (out_dir / "peek.txt").write_text(text, encoding="utf-8")
        except (AttributeError, OSError, RuntimeError) as exc:
            (out_dir / "peek.error.txt").write_text(repr(exc), encoding="utf-8")

    if ctx.response_raw is not None:
        (out_dir / "response.raw.txt").write_text(ctx.response_raw, encoding="utf-8")
    elif ctx.response is not None:
        (out_dir / "response.raw.txt").write_text(getattr(ctx.response, "raw", "") or "", encoding="utf-8")

    return out_dir
