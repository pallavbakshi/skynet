from __future__ import annotations

from typing import Any

import pytest

from smallops import HerdrMux, SessionInfo


@pytest.fixture(autouse=True)
def no_herdr_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("smallops.mux._herdr.sleep", lambda _: None)


@pytest.mark.offline
def test_herdr_mux_create_session_parses_workspace_payload() -> None:
    mux = HerdrMux(auto_start=False, socket_path="/tmp/missing-herdr.sock")

    def fake_json(args: list[str], *, allow_failure: bool = False) -> dict[str, Any]:
        if args == ["workspace", "list"]:
            return {"result": {"workspaces": []}}
        if args[:3] == ["workspace", "create", "--label"]:
            assert args == [
                "workspace",
                "create",
                "--label",
                "smallops-demo",
                "--no-focus",
                "--cwd",
                "/repo",
            ]
            return {
                "result": {
                    "root_pane": {"pane_id": "w1:p1", "cwd": "/repo"},
                    "workspace": {"workspace_id": "w1"},
                }
            }
        raise AssertionError(args)

    mux._json = fake_json  # type: ignore[method-assign]

    session = mux.create_session(name="demo", cwd="/repo")

    assert session == SessionInfo(
        id="w1:p1",
        name="demo",
        cwd="/repo",
        metadata={"workspace_id": "w1", "label": "smallops-demo"},
    )


@pytest.mark.offline
def test_herdr_mux_send_text_uses_text_then_enter() -> None:
    mux = HerdrMux(auto_start=False, socket_path="/tmp/missing-herdr.sock")
    calls: list[list[str]] = []
    mux._run = lambda args, **_: calls.append(args) or ""  # type: ignore[method-assign]

    mux.send_text(SessionInfo(id="w1:p1", name="demo"), "hello", enter=True)

    assert calls == [
        ["pane", "send-text", "w1:p1", "hello"],
        ["pane", "send-keys", "w1:p1", "Enter"],
    ]


@pytest.mark.offline
def test_herdr_mux_respawn_runs_command_through_shell_pane() -> None:
    mux = HerdrMux(auto_start=False, socket_path="/tmp/missing-herdr.sock")
    calls: list[list[str]] = []
    mux._run = lambda args, **_: calls.append(args) or ""  # type: ignore[method-assign]

    session = SessionInfo(id="w1:p1", name="demo")
    returned = mux.respawn(session, "claude --dangerously-skip-permissions", env={"TOKEN": "a b"})

    assert returned is session
    assert calls == [
        ["pane", "run", "w1:p1", "env TOKEN='a b' claude --dangerously-skip-permissions"],
    ]


@pytest.mark.offline
def test_herdr_mux_shell_idle_uses_foreground_process_name() -> None:
    mux = HerdrMux(auto_start=False, socket_path="/tmp/missing-herdr.sock")

    mux._json = lambda *_, **__: {  # type: ignore[method-assign]
        "result": {"process_info": {"foreground_processes": [{"name": "bash"}]}}
    }
    assert mux.shell_idle(SessionInfo(id="w1:p1", name="demo")) is True

    mux._json = lambda *_, **__: {  # type: ignore[method-assign]
        "result": {"process_info": {"foreground_processes": [{"name": "node"}]}}
    }
    assert mux.shell_idle(SessionInfo(id="w1:p1", name="demo")) is False
