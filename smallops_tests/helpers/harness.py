"""Declarative test harness and oracles for smallops."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from tomllib import TOMLDecodeError
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from smallops import (
    AgentState,
    ClaudeCodeTui,
    CodexTui,
    Config,
    HerdrMux,
    IdleReason,
    Response,
    Session,
    TmuxMux,
    WezTermMux,
)
from smallops_tests.helpers.artifacts import record_context, snapshot_context

PROVIDER_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "API_TIMEOUT_MS",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "SMALLOPS_CODEX_OPENROUTER_API_KEY",
    "SMALLOPS_CODEX_MODEL",
    "AGP_CODEX_MODEL",
)

SMALLOPS_MUXES = ("tmux", "wezterm", "herdr")
CODEX_PROVIDER_PREFLIGHT: dict[str, tuple[bool, str]] = {}


class Oracle(Protocol):
    def verify(self, ctx: RunContext) -> None: ...


@dataclass(frozen=True, slots=True)
class Spec:
    prompt: str
    oracle: Oracle
    environment: str = "live"
    tui: str = "claude_code"
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
        if which("tmux") is None:
            pytest.skip("tmux is not installed")
        return TmuxMux(prefix="smallops-test")
    if kind == "wezterm":
        if os.environ.get("SMALLOPS_DOCKER") != "1":
            _require_local_wezterm_mux()
        return WezTermMux(workspace="smallops-test")
    if kind == "herdr":
        if which("herdr") is None:
            pytest.skip("herdr is not installed")
        return HerdrMux(prefix="smallops-test")
    raise ValueError(f"unsupported smallops test mux: {kind}")


def _require_local_wezterm_mux() -> None:
    if which("wezterm") is None:
        pytest.skip("wezterm is not installed")
    try:
        result = subprocess.run(
            ["wezterm", "cli", "list", "--format", "json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"wezterm mux server is not reachable: {exc!r}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        pytest.skip(f"wezterm mux server is not reachable: {detail}")


def make_tui(environment: str = "live", tui: str = "claude_code"):
    if tui == "claude_code" and environment == "docker":
        return ClaudeCodeTui(flags="--dangerously-skip-permissions --model sonnet")
    if tui == "claude_code":
        return ClaudeCodeTui()
    if tui == "codex":
        model = (
            os.environ.get("SMALLOPS_CODEX_LIVE_MODEL")
            or os.environ.get("SMALLOPS_CODEX_MODEL")
            or os.environ.get("AGP_CODEX_MODEL")
            or ""
        )
        extra_flags = (
            os.environ.get("SMALLOPS_CODEX_LIVE_FLAGS")
            if environment == "live" and "SMALLOPS_CODEX_LIVE_FLAGS" in os.environ
            else os.environ.get("SMALLOPS_CODEX_FLAGS", "")
        )
        if environment == "docker":
            model = (
                os.environ.get("SMALLOPS_CODEX_MODEL")
                or os.environ.get("AGP_CODEX_MODEL")
                or "openai/gpt-5.3-codex"
            )
            flags = (
                "-p openrouter --dangerously-bypass-approvals-and-sandbox "
                f"--model {model}"
            )
            if extra_flags:
                flags = f"{flags} {extra_flags}"
            return CodexTui(
                send_via_launch=True,
                defer_launch_until_send=True,
                script_pty=True,
                flags=flags,
            )
        flags = extra_flags
        if model:
            flags = f"{flags} --model {model}".strip()
        return CodexTui(flags=flags)
    raise ValueError(f"unsupported smallops test tui: {tui}")


def provider_env() -> dict[str, str]:
    return {key: os.environ[key] for key in PROVIDER_ENV_KEYS if key in os.environ}


def run_spec(spec: Spec, *, request: pytest.FixtureRequest, tmp_path: Path) -> RunContext:
    if spec.environment not in {"live", "docker"}:
        raise ValueError(f"run_spec supports live/docker specs, got {spec.environment!r}")

    if spec.environment == "docker" and spec.tui == "codex":
        trust_codex_project(tmp_path)
        require_codex_provider_route()

    session = Session(
        mux=make_mux(spec.mux),
        tui=make_tui(spec.environment, spec.tui),
        config=Config(poll_interval=1.0, idle_threshold=2, timeout=120.0, bootstrap_timeout=90.0),
    )
    record_context(request.node, session=session)
    response: Response | None = None
    try:
        session.up(cwd=str(tmp_path), env=provider_env())
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
    assert_no_provider_error(ctx)
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
    if response.parsed is not None:
        parsed_status = response.parsed.status
        if not status.model and parsed_status.model:
            status.model = parsed_status.model
        if status.tokens <= 0 and parsed_status.tokens > 0:
            status.tokens = parsed_status.tokens
    if not status.model or status.tokens <= 0:
        raw_status = ctx.session.tui.parse_status(response.raw)
        if not status.model and raw_status.model:
            status.model = raw_status.model
        if status.tokens <= 0 and raw_status.tokens > 0:
            status.tokens = raw_status.tokens
    assert status.model
    assert isinstance(status.tokens, int)
    if ctx.spec.environment == "docker":
        assert status.tokens >= 0
    else:
        assert status.tokens > 0

    second = ctx.session.meta()
    assert second.alive
    if second.idle_reason is not None:
        assert second.idle_reason == IdleReason.READY


def trust_codex_project(path: Path) -> None:
    """Pre-trust the exact Docker pytest cwd for Codex.

    Codex 0.146.0 does not treat a trusted parent such as ``/tmp`` as covering
    pytest's per-test directories. Writing the exact project entry keeps the
    Docker canary focused on the smallops mux/TUI path rather than Codex
    onboarding.
    """

    trusted = path.resolve()
    homes = [
        Path.home() / ".codex" / "config.toml",
        Path.home() / ".config" / "codex" / "config.toml",
    ]
    for config_path in homes:
        if config_path.exists():
            _append_codex_trust_block(config_path, trusted)


def _append_codex_trust_block(config_path: Path, trusted: Path) -> None:
    key = f'[projects."{_toml_string_key(str(trusted))}"]'
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return
    if key in text:
        return

    try:
        import tomllib

        tomllib.loads(text)
    except (TOMLDecodeError, UnicodeDecodeError):
        return

    suffix = "" if text.endswith("\n") else "\n"
    config_path.write_text(f'{text}{suffix}\n{key}\ntrust_level = "trusted"\n', encoding="utf-8")


def _toml_string_key(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def require_codex_provider_route() -> None:
    if os.environ.get("SMALLOPS_CODEX_PROVIDER_PREFLIGHT", "1") in {"0", "false", "False"}:
        return

    model = (
        os.environ.get("SMALLOPS_CODEX_MODEL")
        or os.environ.get("AGP_CODEX_MODEL")
        or "openai/gpt-5.3-codex"
    )
    ok, detail = CODEX_PROVIDER_PREFLIGHT.get(model, (False, ""))
    if not detail:
        ok, detail = _probe_openrouter_model(model)
        CODEX_PROVIDER_PREFLIGHT[model] = (ok, detail)
    if not ok:
        pytest.skip(
            f"OpenRouter route for Codex Docker model {model!r} is not accepting requests: {detail}. "
            "Set SMALLOPS_CODEX_PROVIDER_PREFLIGHT=0 to force the full TUI run."
        )


def _probe_openrouter_model(model: str) -> tuple[bool, str]:
    api_key = _openrouter_api_key()
    if not api_key:
        return False, "OPENROUTER_API_KEY/ANTHROPIC_AUTH_TOKEN is unset"

    payload = json.dumps(
        {
            "model": model,
            "input": "Reply with OK.",
            "reasoning": {"effort": "low"},
            "max_output_tokens": 16,
        }
    ).encode()
    request = Request(
        "https://openrouter.ai/api/v1/responses",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/pb/skynet",
            "X-Title": "skynet smallops docker tests",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        return False, _summarize_openrouter_error(exc.code, body)
    except (OSError, URLError) as exc:
        return False, repr(exc)


def _summarize_openrouter_error(status: int, body: str) -> str:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return f"HTTP {status}: {body[:240]}"
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if message:
            return f"HTTP {status} code={code}: {message}"
    return f"HTTP {status}: {body[:240]}"


def _openrouter_api_key() -> str | None:
    return (
        os.environ.get("SMALLOPS_CODEX_OPENROUTER_API_KEY")
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


def assert_tool_use(ctx: RunContext) -> None:
    assert ctx.response.parsed is not None
    assert ctx.response.parsed.tool_uses, "expected at least one parsed tool-use block"


def assert_visible_tool_activity(ctx: RunContext) -> None:
    assert ctx.response.parsed is not None
    if ctx.response.parsed.tool_uses:
        return

    raw = ctx.response.raw.casefold()
    tool_summaries = (
        "read 1 file",
        "read 2 files",
        "read 3 files",
        "edited 1 file",
        "edited 2 files",
        "wrote 1 file",
        "wrote 2 files",
        "ran 1 command",
        "ran 2 commands",
    )
    assert any(summary in raw for summary in tool_summaries), (
        "expected expanded tool-use blocks or a collapsed Claude Code tool summary"
    )


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


def assert_no_provider_error(ctx: RunContext) -> None:
    raw = ctx.response.raw
    lower = raw.casefold()
    provider_error_markers = (
        "unexpected status",
        "403 forbidden",
        "401 unauthorized",
        "429 too many requests",
        "provider terms of service",
        "model_not_found",
        "insufficient credits",
    )
    for marker in provider_error_markers:
        if marker in lower:
            excerpt = "\n".join(line for line in raw.splitlines() if marker in line.casefold())
            if not excerpt:
                excerpt = raw[-1000:]
            raise AssertionError(f"provider/API error surfaced in TUI output:\n{excerpt}")


def assert_response_contains(needle: str, *, case_sensitive: bool = False) -> Callable[[RunContext], None]:
    def _assert(ctx: RunContext) -> None:
        Contains(needle, case_sensitive=case_sensitive).verify(ctx)

    return _assert
