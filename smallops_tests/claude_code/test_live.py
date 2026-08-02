"""Env-gated live canaries for the smallops Claude Code driver."""

from __future__ import annotations

from pathlib import Path

import pytest

from smallops import ClaudeCodeTui, Config, IdleReason, Session
from smallops_tests.helpers.artifacts import record_context, snapshot_context
from smallops_tests.helpers.harness import (
    AllOf,
    Contains,
    Invariant,
    Spec,
    assert_tool_use,
    make_mux,
    provider_env,
    run_spec,
)


@pytest.mark.live
def test_live_exact_reply(request: pytest.FixtureRequest, tmp_path: Path, smallops_mux: str) -> None:
    spec = Spec(
        prompt="Reply with exactly: PONG",
        oracle=Contains("PONG"),
        mux=smallops_mux,
        timeout=120.0,
    )
    run_spec(spec, request=request, tmp_path=tmp_path)


@pytest.mark.live
def test_live_file_read_tool_use(request: pytest.FixtureRequest, tmp_path: Path, smallops_mux: str) -> None:
    (tmp_path / "README.md").write_text("SKYNET-LIVE-CANARY\n\nBody text.\n", encoding="utf-8")
    spec = Spec(
        prompt="Read README.md and reply with exactly its first line.",
        oracle=AllOf((Invariant(assert_tool_use), Contains("SKYNET-LIVE-CANARY"))),
        mux=smallops_mux,
        timeout=180.0,
    )
    run_spec(spec, request=request, tmp_path=tmp_path)


@pytest.mark.live
def test_live_nudge_then_wait(request: pytest.FixtureRequest, tmp_path: Path, smallops_mux: str) -> None:
    session = Session(
        mux=make_mux(smallops_mux),
        tui=ClaudeCodeTui(),
        config=Config(poll_interval=1.0, idle_threshold=2, timeout=120.0, bootstrap_timeout=90.0),
    )
    record_context(request.node, session=session)
    try:
        session.up(cwd=str(tmp_path), env=provider_env())
        session.nudge("Reply with exactly: NUDGE77")
        reason = session.wait(timeout=120.0)
        assert reason == IdleReason.READY
        output = session.read(n=200)
        assert "NUDGE77" in output.upper()
    except Exception:
        snapshot_context(request.node)
        raise
    finally:
        session.down()
