"""smallops CLI — interact with TUI agent sessions from the command line.

Usage:
    smallops up [--mux tmux|wezterm] [--tui claude|codex] [--cwd PATH] [--name NAME]
    smallops down [--name NAME]
    smallops send PROMPT [--name NAME] [--timeout N]
    smallops peek [--name NAME] [-n LINES]
    smallops read [--name NAME] [-n LINES] [--since last|MARKER]
    smallops interrupt [--name NAME]
    smallops wait [--name NAME] [--timeout N]
    smallops meta [--name NAME]
    smallops nudge TEXT [--name NAME]
    smallops alive [--name NAME]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# State file for tracking active sessions
_STATE_DIR = Path("/tmp/smallops")
_STATE_FILE = _STATE_DIR / "cli-sessions.json"


def _load_sessions() -> dict:
    if _STATE_FILE.exists():
        return json.loads(_STATE_FILE.read_text())
    return {}


def _save_sessions(sessions: dict) -> None:
    _STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(sessions, indent=2))


def _remove_session(name: str) -> None:
    sessions = _load_sessions()
    sessions.pop(name, None)
    _save_sessions(sessions)


def _parse_opts(opts: list[str] | None) -> dict:
    """Parse key=value pairs into a dict."""
    result = {}
    for opt in opts or []:
        if "=" not in opt:
            print(f"Invalid option '{opt}' — expected key=value", file=sys.stderr)
            sys.exit(1)
        key, value = opt.split("=", 1)
        result[key] = value
    return result


def _make_mux(kind: str, opts: dict | None = None):
    from smallops.mux import TmuxMux, WezTermMux
    kw = opts or {}
    if kind == "wezterm":
        return WezTermMux(**kw)
    return TmuxMux(**kw)


def _make_tui(kind: str, opts: dict | None = None):
    from smallops.tui import ClaudeCodeTui, CodexTui
    kw = opts or {}
    if kind == "codex":
        return CodexTui(**kw)
    return ClaudeCodeTui(**kw)


def _reconnect(name: str):
    """Reconnect to an existing session from saved state."""
    from smallops import Session
    from smallops._types import SessionInfo

    sessions = _load_sessions()
    if name not in sessions:
        print(f"No session named '{name}'. Run 'smallops up' first.", file=sys.stderr)
        sys.exit(1)

    info = sessions[name]
    mux = _make_mux(info["mux"], info.get("mux_opts"))
    tui = _make_tui(info["tui"], info.get("tui_opts"))
    s = Session(mux=mux, tui=tui, name=name)
    s._session = SessionInfo(id=info["pane_id"], name=name, cwd=info.get("cwd"))
    s._started_at = 0
    return s


def cmd_up(args):
    from smallops import Session

    mux_opts = _parse_opts(args.mux_opt)
    tui_opts = _parse_opts(args.tui_opt)
    mux = _make_mux(args.mux, mux_opts)
    tui = _make_tui(args.tui, tui_opts)
    name = args.name
    cwd = args.cwd or os.getcwd()

    s = Session(mux=mux, tui=tui, name=name)
    s.up(cwd=cwd)

    # Save session state (including opts for reconnect)
    sessions = _load_sessions()
    sessions[name] = {
        "pane_id": s._session.id,
        "mux": args.mux,
        "tui": args.tui,
        "cwd": cwd,
        "mux_opts": mux_opts,
        "tui_opts": tui_opts,
    }
    _save_sessions(sessions)

    m = s.meta()
    print(f"Session '{name}' is up")
    print(f"  pane:       {m.pane_id}")
    print(f"  mux:        {m.mux}")
    print(f"  tui:        {m.tui}")
    print(f"  model:      {m.status.model}")
    print(f"  effort:     {m.status.effort}")
    print(f"  session_id: {m.status.session_id}")


def cmd_attach(args):
    sessions = _load_sessions()
    name = args.name
    if name not in sessions:
        print(f"No session named '{name}'. Run 'smallops up' first.", file=sys.stderr)
        sys.exit(1)

    info = sessions[name]
    pane_id = info["pane_id"]
    mux = info["mux"]

    if mux == "tmux":
        os.execvp("tmux", ["tmux", "attach", "-t", pane_id])
    elif mux == "wezterm":
        import subprocess
        subprocess.run(["wezterm", "cli", "activate-pane", "--pane-id", pane_id], check=False)
        print(f"Activated wezterm pane {pane_id} (use workspace switcher if not visible)")
    else:
        print(f"Unknown mux: {mux}", file=sys.stderr)
        sys.exit(1)


def cmd_down(args):
    s = _reconnect(args.name)
    s.down()
    _remove_session(args.name)
    print(f"Session '{args.name}' is down")


def cmd_send(args):
    s = _reconnect(args.name)
    prompt = " ".join(args.prompt)
    print(f"Sending to '{args.name}'...")
    r = s.send(prompt, timeout=args.timeout)
    print(r.text)
    print(f"\n--- {r.elapsed:.1f}s ---")


def cmd_peek(args):
    s = _reconnect(args.name)
    print(s.peek(args.n))


def cmd_read(args):
    s = _reconnect(args.name)
    print(s.read(args.n, since=args.since))


def cmd_interrupt(args):
    s = _reconnect(args.name)
    s.interrupt()
    print(f"Interrupted '{args.name}'")


def cmd_wait(args):
    s = _reconnect(args.name)
    print(f"Waiting for '{args.name}' to go idle...")
    reason = s.wait(timeout=args.timeout)
    print(f"Idle: {reason.value}")


def cmd_meta(args):
    s = _reconnect(args.name)
    m = s.meta()
    print(f"state:          {m.state.value}")
    print(f"idle_reason:    {m.idle_reason.value if m.idle_reason else '-'}")
    print(f"alive:          {m.alive}")
    print(f"tui:            {m.tui}")
    print(f"mux:            {m.mux}")
    print(f"pane_id:        {m.pane_id}")
    print(f"model:          {m.status.model}")
    print(f"effort:         {m.status.effort}")
    print(f"session_id:     {m.status.session_id}")
    print(f"tokens:         {m.status.tokens}")
    print(f"context_pct:    {m.status.context_pct}")
    print(f"last_completed: {m.status.last_completed}")


def cmd_nudge(args):
    s = _reconnect(args.name)
    text = " ".join(args.text)
    path = s.nudge(text)
    print(f"Nudge sent to '{args.name}' via {path}")


def cmd_alive(args):
    s = _reconnect(args.name)
    alive = s.is_alive()
    print("yes" if alive else "no")
    sys.exit(0 if alive else 1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="smallops", description="TUI agent session manager")
    sub = parser.add_subparsers(dest="command", required=True)

    # up
    p = sub.add_parser("up", help="Start a new session")
    p.add_argument("--mux", choices=["tmux", "wezterm"], default="tmux")
    p.add_argument("--tui", choices=["claude", "codex"], default="claude")
    p.add_argument("--cwd", help="Working directory")
    p.add_argument("--name", default="default", help="Session name")
    p.add_argument("--mux-opt", action="append", metavar="KEY=VALUE",
                   help="Mux options (e.g. --mux-opt workspace=myws)")
    p.add_argument("--tui-opt", action="append", metavar="KEY=VALUE",
                   help="Tui options (e.g. --tui-opt flags=--full-auto)")
    p.set_defaults(func=cmd_up)

    # attach
    p = sub.add_parser("attach", help="Attach to a session's tmux/wezterm pane")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_attach)

    # down
    p = sub.add_parser("down", help="Stop a session")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_down)

    # send
    p = sub.add_parser("send", help="Send a prompt and wait for response")
    p.add_argument("prompt", nargs="+", help="Prompt text")
    p.add_argument("--name", default="default")
    p.add_argument("--timeout", type=float, default=300.0)
    p.set_defaults(func=cmd_send)

    # peek
    p = sub.add_parser("peek", help="Raw screen capture")
    p.add_argument("-n", type=int, default=None, help="Lines from scrollback")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_peek)

    # read
    p = sub.add_parser("read", help="Parsed response from screen")
    p.add_argument("-n", type=int, default=None, help="Lines from scrollback")
    p.add_argument("--since", default=None, help="Marker or 'last'")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_read)

    # interrupt
    p = sub.add_parser("interrupt", help="Send Ctrl-C")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_interrupt)

    # wait
    p = sub.add_parser("wait", help="Block until idle")
    p.add_argument("--name", default="default")
    p.add_argument("--timeout", type=float, default=300.0)
    p.set_defaults(func=cmd_wait)

    # meta
    p = sub.add_parser("meta", help="Show session metadata")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_meta)

    # nudge
    p = sub.add_parser("nudge", help="Inject text into a running agent (fire-and-forget)")
    p.add_argument("text", nargs="+", help="Nudge text")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_nudge)

    # alive
    p = sub.add_parser("alive", help="Check if session is alive (exit 0/1)")
    p.add_argument("--name", default="default")
    p.set_defaults(func=cmd_alive)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
