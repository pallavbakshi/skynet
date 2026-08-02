"""Docker fresh-first-run canaries for the smallops Claude Code driver."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smallops import (
    AgentState,
    ClaudeCodeTui,
    Config,
    FatalGate,
    IdleReason,
    Session,
    SessionInfo,
)
from smallops_tests.helpers.artifacts import record_context, snapshot_context
from smallops_tests.helpers.harness import (
    AllOf,
    Contains,
    FileContent,
    Invariant,
    Spec,
    assert_visible_tool_activity,
    make_mux,
    make_tui,
    provider_env,
    run_spec,
)
from smallops_tests.helpers.harness import TestPasses as SpecTestPasses

CLAUDE_CODE_VERSION = "2.1.170"
WEZTERM_VERSION = "20260117-154428-05343b38"
BYPASS_ACCEPT = "\x1b[B"


def _require_smallops_container() -> None:
    if Path(os.environ.get("HOME", "")) != Path("/tmp/smallops-home"):
        pytest.skip("requires the smallops Docker test image")


@pytest.mark.docker
def test_docker_00_starts_with_pristine_claude_home() -> None:
    _require_smallops_container()
    home = Path(os.environ["HOME"])
    assert home == Path("/tmp/smallops-home")
    assert home.exists()
    assert not (home / ".claude.json").exists()
    assert not (home / ".claude").exists()


@pytest.mark.docker
def test_docker_image_has_pinned_claude_code_and_tmux() -> None:
    _require_smallops_container()
    claude = subprocess.run(
        ["claude", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15.0,
    )
    assert claude.returncode == 0, claude.stderr or claude.stdout
    assert CLAUDE_CODE_VERSION in claude.stdout + claude.stderr

    tmux = subprocess.run(
        ["tmux", "-V"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15.0,
    )
    assert tmux.returncode == 0, tmux.stderr or tmux.stdout

    wezterm = subprocess.run(
        ["wezterm", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15.0,
    )
    assert wezterm.returncode == 0, wezterm.stderr or wezterm.stdout
    assert WEZTERM_VERSION in wezterm.stdout + wezterm.stderr


@pytest.mark.docker
@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        ("Choose the text style for Claude Code\n❯", ""),
        ("Security notes\nPress Enter to continue", ""),
        ("Bypass Permissions Mode\nI accept all responsibility", BYPASS_ACCEPT),
        ("Quick safety check\nYes, I trust this folder", ""),
        ("Allow Bash command? (y/n)", "y"),
        ("Claude Code update available", ""),
    ],
)
def test_docker_gate_patterns_are_auto_dismissible(screen: str, expected: str) -> None:
    tui = ClaudeCodeTui()
    assert tui.classify_idle(screen) == IdleReason.GATE
    assert tui.gate_response(screen) == expected


@pytest.mark.docker
@pytest.mark.parametrize(
    ("screen", "expected"),
    [
        ("Choose the text style for Claude Code\n❯", ""),
        ("Security notes\nPress Enter to continue", ""),
        ("Bypass Permissions Mode\nI accept all responsibility", BYPASS_ACCEPT),
        ("Quick safety check\nYes, I trust this folder", ""),
        ("Allow Bash command? (y/n)", "y"),
        ("Claude Code update available", ""),
    ],
)
def test_docker_bootstrap_auto_dismisses_each_scripted_gate(screen: str, expected: str) -> None:
    mux = ScriptedGateMux(screens=[screen, READY_SCREEN])
    session = Session(
        mux=mux,
        tui=ClaudeCodeTui(cli="claude"),
        config=Config(poll_interval=0.0, idle_threshold=1, bootstrap_timeout=2.0),
    )
    try:
        session.up(cwd="/tmp")
    finally:
        session.down()

    assert mux.sent == [(expected, True)]


@pytest.mark.docker
@pytest.mark.parametrize(
    "screen",
    [
        "Select login method\nPaste code here",
        "OAuth error\nBrowser didn't open",
    ],
)
def test_docker_fatal_gate_patterns_are_not_dismissed(screen: str) -> None:
    tui = ClaudeCodeTui()
    assert tui.is_fatal_gate(screen)
    assert tui.gate_response(screen) is None


@pytest.mark.docker
def test_docker_bootstrap_auto_dismisses_scripted_gate_sequence() -> None:
    mux = ScriptedGateMux(
        screens=[
            "Choose the text style for Claude Code\n❯",
            "Quick safety check\nYes, I trust this folder",
            READY_SCREEN,
        ],
    )
    session = Session(
        mux=mux,
        tui=ClaudeCodeTui(cli="claude"),
        config=Config(poll_interval=0.0, idle_threshold=1, bootstrap_timeout=2.0),
    )
    try:
        session.up(cwd="/tmp")
    finally:
        session.down()

    assert mux.sent == [("", True), ("", True)]


@pytest.mark.docker
@pytest.mark.parametrize(
    "screen",
    [
        "Select login method\nPaste code here",
        "OAuth error\nBrowser didn't open",
    ],
)
def test_docker_bootstrap_stops_on_fatal_gate(screen: str) -> None:
    mux = ScriptedGateMux(screens=[screen])
    session = Session(
        mux=mux,
        tui=ClaudeCodeTui(cli="claude"),
        config=Config(poll_interval=0.0, idle_threshold=1, bootstrap_timeout=2.0),
    )
    with pytest.raises(FatalGate):
        session.up(cwd="/tmp")
    session.down()
    assert mux.sent == []


@pytest.mark.docker
@pytest.mark.parametrize("smallops_mux", ["tmux", "wezterm"])
def test_docker_claude_code_exact_reply(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    spec = Spec(
        prompt=(
            "Output one line containing exactly DOCKER-PONG. "
            "Do not create memories, schedules, files, todos, or tool calls. "
            "Do not explain."
        ),
        oracle=Contains("DOCKER-PONG"),
        environment="docker",
        mux=smallops_mux,
        timeout=180.0,
    )
    run_spec(spec, request=request, tmp_path=tmp_path)


@pytest.mark.docker
@pytest.mark.parametrize("smallops_mux", ["tmux", "wezterm"])
def test_docker_claude_code_read_tool_use(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    (tmp_path / "README.md").write_text("SKYNET-DOCKER-TOOL-CANARY\n\nBody text.\n", encoding="utf-8")
    spec = Spec(
        prompt=(
            "Use the Read tool to read README.md. "
            "Reply with exactly the first line and no other text."
        ),
        oracle=AllOf((Invariant(assert_visible_tool_activity), Contains("SKYNET-DOCKER-TOOL-CANARY"))),
        environment="docker",
        mux=smallops_mux,
        timeout=180.0,
    )
    run_spec(spec, request=request, tmp_path=tmp_path)


@pytest.mark.docker
@pytest.mark.parametrize("smallops_mux", ["tmux", "wezterm"])
def test_docker_claude_code_file_write(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    spec = Spec(
        prompt=(
            "Create a file named docker_created.txt containing exactly "
            "DOCKER-FILE-CONTENT and a trailing newline. "
            "Then reply with exactly DOCKER-FILE-DONE."
        ),
        oracle=AllOf((
            FileContent("docker_created.txt", "DOCKER-FILE-CONTENT\n"),
            Contains("DOCKER-FILE-DONE"),
        )),
        environment="docker",
        mux=smallops_mux,
        timeout=180.0,
    )
    run_spec(spec, request=request, tmp_path=tmp_path)


@pytest.mark.docker
@pytest.mark.parametrize("smallops_mux", ["tmux", "wezterm"])
def test_docker_claude_code_test_fix(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_fixture_example.py").write_text(
        "from app_math import add\n\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (tmp_path / "app_math.py").write_text(
        "def add(a, b):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    spec = Spec(
        prompt=(
            "Make tests/test_fixture_example.py pass by editing app_math.py only. "
            "Then reply with exactly DOCKER-TESTS-PASS."
        ),
        oracle=AllOf((
            SpecTestPasses("tests/test_fixture_example.py", timeout=30.0),
            Contains("DOCKER-TESTS-PASS"),
        )),
        environment="docker",
        mux=smallops_mux,
        timeout=240.0,
    )
    run_spec(spec, request=request, tmp_path=tmp_path)


@pytest.mark.docker
@pytest.mark.parametrize("smallops_mux", ["tmux", "wezterm"])
def test_docker_session_api_surface(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    session = Session(
        mux=make_mux(smallops_mux),
        tui=make_tui("docker"),
        config=Config(poll_interval=1.0, idle_threshold=2, timeout=180.0, bootstrap_timeout=90.0),
    )
    record_context(request.node, session=session)

    try:
        info = session.up(cwd=str(tmp_path), env=provider_env())
        assert info.id
        assert info.cwd == str(tmp_path)
        assert session.is_alive()

        meta = session.meta()
        assert meta.alive
        assert meta.mux == smallops_mux
        assert meta.tui == "claude_code"
        assert meta.pane_id == info.id

        first = session.send(
            _literal_prompt("API-SEND-ONE"),
            sections="## Test Metadata\nsurface=send-sections\n",
            timeout=180.0,
        )
        record_context(request.node, session=session, response=first)
        assert "API-SEND-ONE" in first.text
        assert first.marker
        assert first.raw
        assert first.elapsed > 0

        parsed_read = session.read(n=200, since="last")
        assert "API-SEND-ONE" in parsed_read
        marker_read = session.read(n=200, since=first.marker)
        assert "API-SEND-ONE" in marker_read
        visible = session.peek()
        assert "API-SEND-ONE" in visible
        tail = session.peek(n=120)
        assert "API-SEND-ONE" in tail

        prompt_file = tmp_path / "surface_prompt.txt"
        prompt_file.write_text(_literal_prompt("API-SEND-FILE"), encoding="utf-8")
        from_file = session.send(file=str(prompt_file), timeout=180.0)
        record_context(request.node, session=session, response=from_file)
        assert "API-SEND-FILE" in from_file.text
        assert "API-SEND-FILE" in session.read(n=300, since="last")

        nudge_path = Path(session.nudge(_literal_prompt("API-NUDGE-WAIT")))
        assert nudge_path.exists()
        reason = session.wait(timeout=180.0)
        assert reason == IdleReason.READY
        after_nudge = session.read(n=300)
        assert "API-NUDGE-WAIT" in after_nudge

        old_pane = info.id
        reset_info = session.reset(env=provider_env())
        assert reset_info.id
        if smallops_mux == "wezterm":
            assert reset_info.id != old_pane
        assert session.is_alive()
        assert session.meta().alive

        after_reset = session.send(_literal_prompt("API-AFTER-RESET"), timeout=180.0)
        record_context(request.node, session=session, response=after_reset)
        assert "API-AFTER-RESET" in after_reset.text

        session.interrupt()
        assert session.wait(timeout=60.0) == IdleReason.READY
        assert session.is_alive()

        session.down()
        assert not session.is_alive()
        session.down()
        assert not session.is_alive()
    except Exception:
        snapshot_context(request.node)
        raise
    finally:
        session.down()


@pytest.mark.docker
@pytest.mark.parametrize("smallops_mux", ["tmux", "wezterm"])
def test_docker_interrupts_active_turn(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    session = Session(
        mux=make_mux(smallops_mux),
        tui=make_tui("docker"),
        config=Config(poll_interval=1.0, idle_threshold=2, timeout=180.0, bootstrap_timeout=90.0),
    )
    record_context(request.node, session=session)
    try:
        session.up(cwd=str(tmp_path), env=provider_env())
        session.nudge(
            "Use Bash to run: python -c \"import time; time.sleep(45)\". "
            "Do not reply until the command completes."
        )

        saw_working = False
        for _ in range(20):
            meta = session.meta()
            if meta.state == AgentState.WORKING:
                saw_working = True
                break

        assert saw_working, "expected Claude Code to enter WORKING before interrupt"
        session.interrupt()
        assert session.wait(timeout=120.0) == IdleReason.READY
        assert session.is_alive()
    except Exception:
        snapshot_context(request.node)
        raise
    finally:
        session.down()


def _literal_prompt(token: str) -> str:
    return (
        f"Output one line containing exactly {token}. "
        "Do not create memories, schedules, files, todos, or tool calls. "
        "Do not explain."
    )


@dataclass(slots=True)
class ScriptedGateMux:
    screens: list[str]
    sent: list[tuple[str, bool]] = field(default_factory=list)
    destroyed: bool = False

    kind: str = "scripted"

    def create_session(self, *, name: str, cwd: str | None = None) -> SessionInfo:
        return SessionInfo(id="scripted-pane", name=name, cwd=cwd)

    def destroy_session(self, session: SessionInfo) -> None:
        self.destroyed = True

    def session_exists(self, session: SessionInfo) -> bool:
        return not self.destroyed

    def send_text(self, session: SessionInfo, text: str, *, enter: bool = True) -> None:
        self.sent.append((text, enter))
        if len(self.screens) > 1:
            self.screens.pop(0)

    def peek(self, session: SessionInfo, n: int | None = None) -> str:
        return self.screens[0]

    def shell_idle(self, session: SessionInfo) -> bool:
        return False

    def respawn(self, session: SessionInfo, command: str, *, env: dict[str, str] | None = None) -> SessionInfo:
        return session

    def interrupt(self, session: SessionInfo) -> None:
        return None


READY_SCREEN = "╭─── Claude Code v2.1.170 ───╮\n│ Opus 4.6 · 10 tokens │\n❯"
