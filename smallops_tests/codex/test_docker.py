"""Docker canaries for the smallops Codex driver."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from smallops import (
    AgentState,
    CodexTui,
    Config,
    FatalGate,
    IdleReason,
    Session,
    SessionInfo,
    normalize_screen,
    strip_ansi,
)
from smallops_tests.helpers.artifacts import record_context, snapshot_context
from smallops_tests.helpers.harness import (
    AllOf,
    Contains,
    FileContent,
    Invariant,
    Spec,
    assert_tool_use,
    make_mux,
    make_tui,
    provider_env,
    run_spec,
    trust_codex_project,
)
from smallops_tests.helpers.harness import TestPasses as SpecTestPasses

CODEX_VERSION = "0.146.0"
WEZTERM_VERSION = "20260117-154428-05343b38"
READY_SCREEN = """
╭────────────────────────────────────────╮
│ >_ OpenAI Codex                        │
│ model: openai/gpt-5.6-luna-pro low     │
╰────────────────────────────────────────╯

› Find and fix a bug in @filename
"""


def _require_smallops_container() -> None:
    if Path(os.environ.get("HOME", "")) != Path("/tmp/smallops-home"):
        pytest.skip("requires the smallops Docker test image")


@pytest.mark.docker
def test_docker_00_starts_with_pristine_codex_home() -> None:
    _require_smallops_container()
    home = Path(os.environ["HOME"])
    assert home == Path("/tmp/smallops-home")
    assert home.exists()

    codex_home = home / ".codex"
    config_home = home / ".config" / "codex"
    assert codex_home.exists()
    assert config_home.exists()

    allowed = {
        ".codex/config.toml",
        ".codex/openrouter.config.toml",
        ".codex/smallops-model-catalog.json",
        ".config/codex/config.toml",
        ".config/codex/openrouter.config.toml",
        ".config/codex/smallops-model-catalog.json",
    }
    actual = {
        str(path.relative_to(home))
        for root in (codex_home, config_home)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert actual == allowed
    assert not (codex_home / "auth.json").exists()
    assert not (codex_home / "history.jsonl").exists()
    assert not (codex_home / "sessions").exists()


@pytest.mark.docker
def test_docker_image_has_pinned_codex_and_muxes() -> None:
    _require_smallops_container()
    codex = subprocess.run(
        ["codex", "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15.0,
    )
    assert codex.returncode == 0, codex.stderr or codex.stdout
    assert CODEX_VERSION in codex.stdout + codex.stderr

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
        ("Do you trust the contents of this directory?\n1. Yes, continue", "1"),
        ("Would you like to run the following command?\n1. Yes, proceed", "y"),
        ("Press enter to continue", ""),
        ("Approaching rate limits\n3. Keep current model", "3"),
    ],
)
def test_docker_bootstrap_auto_dismisses_each_scripted_codex_gate(screen: str, expected: str) -> None:
    mux = ScriptedGateMux(screens=[screen, READY_SCREEN])
    session = Session(
        mux=mux,
        tui=CodexTui(cli="codex"),
        config=Config(poll_interval=0.0, idle_threshold=1, bootstrap_timeout=2.0),
    )
    try:
        session.up(cwd="/tmp")
    finally:
        session.down()

    assert mux.sent == [(expected, True)]


@pytest.mark.docker
def test_docker_bootstrap_auto_dismisses_scripted_codex_gate_sequence() -> None:
    mux = ScriptedGateMux(
        screens=[
            "Do you trust the contents of this directory?\n1. Yes, continue",
            "Press enter to continue",
            READY_SCREEN,
        ],
    )
    session = Session(
        mux=mux,
        tui=CodexTui(cli="codex"),
        config=Config(poll_interval=0.0, idle_threshold=1, bootstrap_timeout=2.0),
    )
    try:
        session.up(cwd="/tmp")
    finally:
        session.down()

    assert mux.sent == [("1", True), ("", True)]


@pytest.mark.docker
def test_docker_bootstrap_retries_still_visible_codex_enter_gate() -> None:
    mux = ScriptedGateMux(
        screens=[
            "Press enter to continue",
            "Press enter to continue",
            READY_SCREEN,
        ],
    )
    session = Session(
        mux=mux,
        tui=CodexTui(cli="codex"),
        config=Config(poll_interval=0.0, idle_threshold=1, bootstrap_timeout=2.0),
    )
    try:
        session.up(cwd="/tmp")
    finally:
        session.down()

    assert mux.sent == [("", True), ("", True)]


@pytest.mark.docker
def test_docker_bootstrap_stops_on_fatal_codex_gate() -> None:
    mux = ScriptedGateMux(screens=["You've hit your usage limit. Upgrade to Pro."])
    session = Session(
        mux=mux,
        tui=CodexTui(cli="codex"),
        config=Config(poll_interval=0.0, idle_threshold=1, bootstrap_timeout=2.0),
    )
    with pytest.raises(FatalGate):
        session.up(cwd="/tmp")
    session.down()
    assert mux.sent == []


@pytest.mark.docker
def test_docker_codex_exact_reply(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    spec = Spec(
        prompt="Output one line containing exactly CODEX-DOCKER-PONG. Do not explain.",
        oracle=Contains("CODEX-DOCKER-PONG"),
        environment="docker",
        tui="codex",
        mux=smallops_mux,
        timeout=180.0,
    )
    ctx = run_spec(spec, request=request, tmp_path=tmp_path)
    _capture_corpus("ready", f"post_response_{smallops_mux}", ctx.response.raw)
    _capture_corpus("turns", f"exact_reply_{smallops_mux}", ctx.response.raw)


@pytest.mark.docker
def test_docker_codex_file_read_tool_use(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    (tmp_path / "README.md").write_text("SKYNET-CODEX-DOCKER-CANARY\n\nBody text.\n", encoding="utf-8")
    spec = Spec(
        prompt="Read README.md and reply with exactly its first line.",
        oracle=AllOf((Invariant(assert_tool_use), Contains("SKYNET-CODEX-DOCKER-CANARY"))),
        environment="docker",
        tui="codex",
        mux=smallops_mux,
        timeout=180.0,
    )
    ctx = run_spec(spec, request=request, tmp_path=tmp_path)
    _capture_corpus("turns", f"tool_read_{smallops_mux}", ctx.response.raw)


@pytest.mark.docker
def test_docker_codex_file_write(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    spec = Spec(
        prompt=(
            "Create a file named codex_created.txt containing exactly "
            "CODEX-FILE-CONTENT and a trailing newline. "
            "Then reply with exactly CODEX-FILE-DONE."
        ),
        oracle=AllOf((
            FileContent("codex_created.txt", "CODEX-FILE-CONTENT\n"),
            Contains("CODEX-FILE-DONE"),
        )),
        environment="docker",
        tui="codex",
        mux=smallops_mux,
        timeout=240.0,
    )
    ctx = run_spec(spec, request=request, tmp_path=tmp_path)
    _capture_corpus("turns", f"file_write_{smallops_mux}", ctx.response.raw)


@pytest.mark.docker
def test_docker_codex_test_fix(
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
            "Then reply with exactly CODEX-TESTS-PASS."
        ),
        oracle=AllOf((
            SpecTestPasses("tests/test_fixture_example.py", timeout=30.0),
            Contains("CODEX-TESTS-PASS"),
        )),
        environment="docker",
        tui="codex",
        mux=smallops_mux,
        timeout=240.0,
    )
    ctx = run_spec(spec, request=request, tmp_path=tmp_path)
    _capture_corpus("turns", f"test_fix_{smallops_mux}", ctx.response.raw)


@pytest.mark.docker
def test_docker_codex_session_api_surface(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    trust_codex_project(tmp_path)
    session = Session(
        mux=make_mux(smallops_mux),
        tui=make_tui("docker", "codex"),
        config=Config(poll_interval=1.0, idle_threshold=2, timeout=180.0, bootstrap_timeout=90.0),
    )
    record_context(request.node, session=session)

    try:
        info = session.up(cwd=str(tmp_path), env=provider_env())
        assert info.id
        assert info.cwd == str(tmp_path)

        first = session.send(_literal_prompt("CODEX-API-SEND-ONE"), timeout=180.0)
        record_context(request.node, session=session, response=first)
        _capture_corpus("turns", f"api_send_{smallops_mux}", first.raw)
        assert "CODEX-API-SEND-ONE" in first.text
        assert first.marker
        assert first.raw
        assert first.elapsed > 0
        assert session.is_alive()

        meta = session.meta()
        assert meta.alive
        assert meta.mux == smallops_mux
        assert meta.tui == "codex"
        assert meta.pane_id == session._session.id

        parsed_read = session.read(n=300, since="last")
        assert "CODEX-API-SEND-ONE" in parsed_read
        marker_read = session.read(n=300, since=first.marker)
        assert "CODEX-API-SEND-ONE" in marker_read
        visible = session.peek()
        assert "CODEX-API-SEND-ONE" in visible
        tail = session.peek(n=160)
        assert "CODEX-API-SEND-ONE" in tail

        prompt_file = tmp_path / "codex_surface_prompt.txt"
        prompt_file.write_text(_literal_prompt("CODEX-API-SEND-FILE"), encoding="utf-8")
        from_file = session.send(file=str(prompt_file), timeout=180.0)
        record_context(request.node, session=session, response=from_file)
        assert "CODEX-API-SEND-FILE" in from_file.text
        assert "CODEX-API-SEND-FILE" in session.read(n=300, since="last")

        nudge_path = Path(session.nudge(_literal_prompt("CODEX-API-NUDGE-WAIT")))
        assert nudge_path.exists()
        reason = session.wait(timeout=180.0)
        assert reason == IdleReason.READY
        after_nudge = session.read(n=400)
        assert "CODEX-API-NUDGE-WAIT" in after_nudge

        old_pane = session._session.id
        reset_info = session.reset(env=provider_env())
        assert reset_info.id
        if smallops_mux == "wezterm":
            assert reset_info.id != old_pane

        after_reset = session.send(_literal_prompt("CODEX-API-AFTER-RESET"), timeout=180.0)
        record_context(request.node, session=session, response=after_reset)
        _capture_corpus("turns", f"after_reset_{smallops_mux}", after_reset.raw)
        assert "CODEX-API-AFTER-RESET" in after_reset.text
        assert session.is_alive()
        assert session.meta().alive

        # Codex exits on Ctrl-C at an idle prompt. The active-turn interrupt
        # behavior is covered separately; here we only require the control
        # method to be callable before idempotent teardown.
        session.interrupt()

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
def test_docker_codex_interrupts_active_turn(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    smallops_mux: str,
) -> None:
    _require_smallops_container()
    trust_codex_project(tmp_path)
    session = Session(
        mux=make_mux(smallops_mux),
        tui=make_tui("docker", "codex"),
        config=Config(poll_interval=1.0, idle_threshold=2, timeout=180.0, bootstrap_timeout=90.0),
    )
    record_context(request.node, session=session)
    try:
        session.up(cwd=str(tmp_path), env=provider_env())
        warmup = session.send(_literal_prompt("CODEX-INTERRUPT-WARMUP"), timeout=180.0)
        record_context(request.node, session=session, response=warmup)

        session.nudge(
            "Use the shell to run: python -c \"import time; time.sleep(45)\". "
            "Do not reply until the command completes."
        )

        saw_working = False
        for _ in range(20):
            meta = session.meta()
            if meta.state == AgentState.WORKING:
                saw_working = True
                _capture_corpus("working", f"active_turn_{smallops_mux}", session.peek())
                break

        assert saw_working, "expected Codex to enter WORKING before interrupt"
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


def _capture_corpus(category: str, name: str, screen: str) -> None:
    root = os.environ.get("SMALLOPS_CODEX_CORPUS_OUT")
    if not root:
        return
    out_dir = Path(root) / category
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = screen if screen.endswith("\n") else f"{screen}\n"
    (out_dir / f"{name}.raw").write_text(raw, encoding="utf-8", errors="replace")
    normalized = normalize_screen(strip_ansi(screen))
    if not normalized.endswith("\n"):
        normalized += "\n"
    (out_dir / f"{name}.txt").write_text(normalized, encoding="utf-8", errors="replace")


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
