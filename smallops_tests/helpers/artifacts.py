"""Failure artifact capture for smallops live-style tests."""

from __future__ import annotations

import os
import subprocess
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
    mux_diagnostics: dict[str, str] | None = None


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
    if ctx.session is not None and ctx.mux_diagnostics is None:
        ctx.mux_diagnostics = _collect_mux_diagnostics(ctx.session)
    if ctx.response is not None and ctx.response_raw is None:
        ctx.response_raw = getattr(ctx.response, "raw", "") or ""
    item.stash[ARTIFACT_CONTEXT] = ctx


def dump_failure_artifacts(item: pytest.Item) -> Path | None:
    ctx = item.stash.get(ARTIFACT_CONTEXT, ArtifactContext())
    if ctx.session is None and ctx.response is None and ctx.peek_text is None and ctx.response_raw is None:
        return None

    out_dir = _artifact_dir(item)
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

    if ctx.mux_diagnostics:
        diag_dir = out_dir / "mux"
        diag_dir.mkdir(exist_ok=True)
        for name, text in ctx.mux_diagnostics.items():
            (diag_dir / name).write_text(text, encoding="utf-8", errors="replace")

    return out_dir


def dump_docker_diagnostics(item: pytest.Item, *, out_dir: Path | None = None) -> Path | None:
    base_dir = out_dir or _artifact_dir(item)
    base_dir.mkdir(parents=True, exist_ok=True)
    diag_dir = base_dir / "docker"
    diag_dir.mkdir(exist_ok=True)

    _write_text(diag_dir / "env.txt", _docker_env_report())
    _write_command(diag_dir / "claude-version.txt", ["claude", "--version"])
    _write_command(diag_dir / "codex-version.txt", ["codex", "--version"])
    _write_command(diag_dir / "tmux-version.txt", ["tmux", "-V"])
    _write_command(diag_dir / "wezterm-version.txt", ["wezterm", "--version"])
    _write_command(diag_dir / "herdr-version.txt", ["herdr", "--version"])
    _write_command(diag_dir / "tmux-sessions.txt", ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}:#{session_windows}"])
    _write_command(diag_dir / "tmux-panes.txt", ["tmux", "list-panes", "-a", "-F", "#{session_name}:#{pane_id}:#{pane_current_command}:#{pane_dead}:#{pane_tty}:#{pane_current_path}"])
    _write_command(diag_dir / "wezterm-panes.json", ["wezterm", "cli", "list", "--format", "json"])
    _write_command(diag_dir / "processes.txt", ["ps", "-eo", "pid,ppid,stat,comm,args"])
    _write_command(diag_dir / "wezterm-mux-server.log", ["cat", "/tmp/wezterm-mux-server.log"])
    _write_command(diag_dir / "codex-config.toml", ["cat", str(Path.home() / ".codex" / "config.toml")])
    _write_command(
        diag_dir / "codex-openrouter-config.toml",
        ["cat", str(Path.home() / ".codex" / "openrouter.config.toml")],
    )
    _write_command(
        diag_dir / "codex-model-catalog.json",
        ["cat", str(Path.home() / ".codex" / "smallops-model-catalog.json")],
    )
    _write_command(diag_dir / "codex-debug-models.txt", ["codex", "debug", "models"])
    _write_command(diag_dir / "codex-script-typescript.txt", ["cat", "/tmp/smallops-codex.typescript"])
    _write_command(diag_dir / "codex-home-files.txt", ["find", str(Path.home() / ".codex"), "-maxdepth", "4", "-type", "f"])
    _write_tree(diag_dir / "home-tree.txt", Path(os.environ.get("HOME", "")))

    ctx = item.stash.get(ARTIFACT_CONTEXT, ArtifactContext())
    session = ctx.session
    pane = getattr(session, "_session", None) if session is not None else None
    mux = getattr(session, "mux", None) if session is not None else None
    herdr_env = None
    if mux is not None and getattr(mux, "kind", "") == "herdr":
        try:
            herdr_env = mux._env()
        except (AttributeError, OSError, RuntimeError):
            herdr_env = None
    _write_command(diag_dir / "herdr-workspaces.json", ["herdr", "workspace", "list"], env=herdr_env)
    if pane is not None:
        pane_id = getattr(pane, "id", "")
        _write_command(diag_dir / "tmux-pane-visible.txt", ["tmux", "capture-pane", "-t", pane_id, "-p"])
        _write_command(diag_dir / "tmux-pane-ansi.raw", ["tmux", "capture-pane", "-t", pane_id, "-p", "-e"])
        _write_command(diag_dir / "tmux-pane-scrollback.txt", ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-500"])
        _write_command(diag_dir / "wezterm-pane-visible.txt", ["wezterm", "cli", "get-text", "--pane-id", pane_id])
        _write_command(diag_dir / "wezterm-pane-scrollback.txt", ["wezterm", "cli", "get-text", "--pane-id", pane_id, "--start-line", "-500"])
        _write_command(
            diag_dir / "herdr-pane-visible.txt",
            ["herdr", "pane", "read", pane_id, "--source", "visible"],
            env=herdr_env,
        )
        _write_command(
            diag_dir / "herdr-pane-scrollback.txt",
            ["herdr", "pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", "500"],
            env=herdr_env,
        )
        _write_command(
            diag_dir / "herdr-pane-process-info.json",
            ["herdr", "pane", "process-info", "--pane", pane_id],
            env=herdr_env,
        )

    return diag_dir


def _artifact_dir(item: pytest.Item) -> Path:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    safe_id = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in item.nodeid)
    suffix = f"{ts}-{worker}" if worker else ts
    root = Path(os.environ.get("SMALLOPS_DOCKER_ARTIFACT_DIR", "test-artifacts"))
    return root / f"{safe_id}-{suffix}"


def _docker_env_report() -> str:
    keys = [
        "HOME",
        "PATH",
        "TERM",
        "SMALLOPS_DOCKER",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
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
    ]
    lines = []
    for key in keys:
        if key not in os.environ:
            lines.append(f"{key}=<unset>")
        elif key in {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "SMALLOPS_CODEX_OPENROUTER_API_KEY",
        }:
            value = os.environ[key]
            lines.append(f"{key}=<set len={len(value)}>")
        else:
            lines.append(f"{key}={os.environ[key]}")
    return "\n".join(lines) + "\n"


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", errors="replace")


def _write_command(path: Path, cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    path.write_text(_command_output(cmd, env=env), encoding="utf-8", errors="replace")


def _command_output(cmd: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=15.0,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"$ {' '.join(cmd)}\n<failed: {exc!r}>\n"
    return f"$ {' '.join(cmd)}\nexit={result.returncode}\n\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"


def _collect_mux_diagnostics(session: Any) -> dict[str, str]:
    mux = getattr(session, "mux", None)
    pane = getattr(session, "_session", None)
    if mux is None or pane is None:
        return {}

    kind = getattr(mux, "kind", "")
    pane_id = getattr(pane, "id", "")
    if not pane_id:
        return {}

    if kind == "herdr":
        try:
            env = mux._env()
        except (AttributeError, OSError, RuntimeError):
            env = None
        return {
            "herdr-workspaces.json": _command_output(["herdr", "workspace", "list"], env=env),
            "herdr-pane-visible.txt": _command_output(
                ["herdr", "pane", "read", pane_id, "--source", "visible"],
                env=env,
            ),
            "herdr-pane-scrollback.txt": _command_output(
                ["herdr", "pane", "read", pane_id, "--source", "recent-unwrapped", "--lines", "500"],
                env=env,
            ),
            "herdr-pane-process-info.json": _command_output(
                ["herdr", "pane", "process-info", "--pane", pane_id],
                env=env,
            ),
        }
    if kind == "tmux":
        return {
            "tmux-pane-visible.txt": _command_output(["tmux", "capture-pane", "-t", pane_id, "-p"]),
            "tmux-pane-ansi.raw": _command_output(["tmux", "capture-pane", "-t", pane_id, "-p", "-e"]),
            "tmux-pane-scrollback.txt": _command_output(["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-500"]),
        }
    if kind == "wezterm":
        return {
            "wezterm-pane-visible.txt": _command_output(["wezterm", "cli", "get-text", "--pane-id", pane_id]),
            "wezterm-pane-scrollback.txt": _command_output(
                ["wezterm", "cli", "get-text", "--pane-id", pane_id, "--start-line", "-500"]
            ),
        }
    return {}


def _write_tree(path: Path, root: Path) -> None:
    if not root:
        path.write_text("<HOME unset>\n", encoding="utf-8")
        return
    lines = [f"root={root}"]
    if not root.exists():
        lines.append("<missing>")
    else:
        for child in sorted(root.rglob("*"))[:300]:
            rel = child.relative_to(root)
            kind = "dir" if child.is_dir() else "file"
            lines.append(f"{kind}\t{rel}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", errors="replace")
