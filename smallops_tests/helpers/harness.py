"""Declarative test harness and oracles for smallops."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest

from smallops import (
    AgentState,
    ClaudeCodeTui,
    Config,
    IdleReason,
    Response,
    Session,
    TmuxMux,
)
from smallops_tests.helpers.artifacts import record_context, snapshot_context


class Oracle(Protocol):
    def verify(self, ctx: RunContext) -> None: ...


@dataclass(frozen=True, slots=True)
class Spec:
    prompt: str
    oracle: Oracle
    environment: str = "live"
    mux: str = "tmux"
    timeout: float | None = None
    expect_response: bool = True


@dataclass(slots=True)
class RunContext:
    spec: Spec
    session: Session
    response: Response
    cwd: Path


@dataclass(frozen=True, slots=True)
class Contains:
    needle: str
    case_sensitive: bool = False

    def verify(self, ctx: RunContext) -> None:
        haystack = ctx.response.text if self.case_sensitive else ctx.response.text.casefold()
        needle = self.needle if self.case_sensitive else self.needle.casefold()
        excerpt = ctx.response.text[:500]
        assert needle in haystack, f"missing {self.needle!r} in response text excerpt: {excerpt!r}"


@dataclass(frozen=True, slots=True)
class Exact:
    """Assert the parsed response text exactly matches ``expected``.

    This is intentionally stricter than the normal live canaries. Use it only
    for specs where parser-clean response boundaries are the behavior under
    test; otherwise prefer ``Contains`` plus pollution invariants.
    """

    expected: str
    strip: bool = True
    case_sensitive: bool = True

    def verify(self, ctx: RunContext) -> None:
        actual = ctx.response.text
        expected = self.expected
        if self.strip:
            actual = actual.strip()
            expected = expected.strip()
        if not self.case_sensitive:
            actual = actual.casefold()
            expected = expected.casefold()
        assert actual == expected


@dataclass(frozen=True, slots=True)
class FileContent:
    relpath: str
    expected: str
    strip: bool = False

    def verify(self, ctx: RunContext) -> None:
        rel = Path(self.relpath)
        assert not rel.is_absolute(), f"FileContent relpath must be relative: {self.relpath!r}"
        path = (ctx.cwd / rel).resolve()
        cwd = ctx.cwd.resolve()
        assert path == cwd or path.is_relative_to(cwd), f"FileContent path escapes cwd: {self.relpath!r}"
        assert path.exists(), f"{self.relpath} was not created"
        assert not path.is_symlink(), f"FileContent path must not be a symlink: {self.relpath!r}"
        actual = path.read_text(encoding="utf-8")
        expected = self.expected
        if self.strip:
            actual = actual.strip()
            expected = expected.strip()
        assert actual == expected


@dataclass(frozen=True, slots=True)
class ExitZero:
    """Run a command in the spec cwd and require exit code 0.

    Use ``"{python}"`` as a command element to run with the same interpreter as
    the active pytest process.
    """

    cmd: list[str]
    timeout: float = 30.0

    def verify(self, ctx: RunContext) -> None:
        cmd = [sys.executable if part == "{python}" else part for part in self.cmd]
        try:
            result = subprocess.run(
                cmd,
                cwd=ctx.cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            raise AssertionError(
                f"command timed out after {self.timeout}s: {cmd!r}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ) from exc
        assert result.returncode == 0, result.stdout + result.stderr


@dataclass(frozen=True, slots=True)
class TestPasses:
    test_path: str
    timeout: float = 60.0

    def verify(self, ctx: RunContext) -> None:
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", self.test_path, "-q"],
                cwd=ctx.cwd,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            raise AssertionError(
                f"pytest target timed out after {self.timeout}s: {self.test_path}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ) from exc
        assert result.returncode == 0, result.stdout + result.stderr


@dataclass(frozen=True, slots=True)
class Invariant:
    predicate: Callable[[RunContext], None]

    def verify(self, ctx: RunContext) -> None:
        self.predicate(ctx)


@dataclass(frozen=True, slots=True)
class AllOf:
    oracles: tuple[Oracle, ...]

    def verify(self, ctx: RunContext) -> None:
        for oracle in self.oracles:
            oracle.verify(ctx)


def make_mux(kind: str):
    if kind == "tmux":
        return TmuxMux(prefix="smallops-test")
    raise ValueError(f"unsupported smallops test mux: {kind}")


def run_spec(spec: Spec, *, request: pytest.FixtureRequest, tmp_path: Path) -> RunContext:
    if spec.environment != "live":
        raise ValueError(f"run_spec currently supports live specs, got {spec.environment!r}")

    session = Session(
        mux=make_mux(spec.mux),
        tui=ClaudeCodeTui(),
        config=Config(poll_interval=1.0, idle_threshold=2, timeout=120.0, bootstrap_timeout=90.0),
    )
    record_context(request.node, session=session)
    response: Response | None = None
    try:
        session.up(cwd=str(tmp_path))
        response = session.send(spec.prompt, timeout=spec.timeout)
        record_context(request.node, session=session, response=response)
        ctx = RunContext(spec=spec, session=session, response=response, cwd=tmp_path)
        assert_live_invariants(ctx)
        spec.oracle.verify(ctx)
        return ctx
    except Exception:
        if response is not None:
            record_context(request.node, session=session, response=response)
        snapshot_context(request.node)
        raise
    finally:
        session.down()


def assert_live_invariants(ctx: RunContext) -> None:
    response = ctx.response
    assert response.parsed is not None
    assert response.raw
    if ctx.spec.expect_response:
        assert response.text.strip()
        assert_no_task_reference_leak(ctx)

    meta = ctx.session.meta()
    assert meta.alive
    assert meta.state in AgentState
    if meta.idle_reason is not None:
        assert meta.idle_reason == IdleReason.READY
    assert ctx.session.tui.classify_idle(response.raw) == IdleReason.READY

    status = meta.status
    if (not status.model or status.tokens <= 0) and response.parsed is not None:
        status = response.parsed.status
    assert status.model
    assert isinstance(status.tokens, int)
    assert status.tokens > 0

    second = ctx.session.meta()
    assert second.alive
    if second.idle_reason is not None:
        assert second.idle_reason == IdleReason.READY


def assert_tool_use(ctx: RunContext) -> None:
    assert ctx.response.parsed is not None
    assert ctx.response.parsed.tool_uses, "expected at least one parsed tool-use block"


def assert_no_task_reference_leak(ctx: RunContext) -> None:
    text = ctx.response.text
    leaked_fragments = (
        ctx.response.marker,
        "/tmp/smallops/task-",
        "BEGIN TASK",
        "END TASK",
        "Execute only the task text",
    )
    for fragment in leaked_fragments:
        excerpt = text[:500]
        assert fragment not in text, f"leaked task reference fragment {fragment!r} in response excerpt: {excerpt!r}"


def assert_response_contains(needle: str, *, case_sensitive: bool = False) -> Callable[[RunContext], None]:
    def _assert(ctx: RunContext) -> None:
        Contains(needle, case_sensitive=case_sensitive).verify(ctx)

    return _assert
