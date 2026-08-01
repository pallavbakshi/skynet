"""Plugin registry for terminal hosts and agent adapters."""
from __future__ import annotations

from typing import Any

from agp.config import settings


def build_terminal_host(kind: str, **kwargs: Any):
    """Instantiate a terminal host plugin by kind name.

    Tmux and WezTerm hosts are backed by smallops Mux implementations
    wrapped in SmallopsTerminalHost for AGP ABC compatibility.
    """
    if kind == "inprocess":
        from agp.plugins.inprocess import InProcessTerminalHost
        return InProcessTerminalHost()
    if kind == "wezterm":
        from agp.plugins._smallops_host import SmallopsTerminalHost
        from smallops import WezTermMux
        mux = WezTermMux(
            workspace=kwargs.get("workspace", settings.wezterm_workspace),
            domain=kwargs.get("domain", settings.wezterm_domain),
            scrollback=kwargs.get("scrollback_lines", settings.scrollback_lines),
        )
        return SmallopsTerminalHost(mux)
    if kind == "tmux":
        from agp.plugins._smallops_host import SmallopsTerminalHost
        from smallops import TmuxMux
        mux = TmuxMux(
            prefix=kwargs.get("session_prefix", settings.tmux_session_prefix),
            scrollback=kwargs.get("scrollback_lines", settings.tmux_scrollback_lines),
        )
        return SmallopsTerminalHost(mux)
    raise ValueError(f"unsupported terminal host kind: {kind}")


def build_agent_adapter(kind: str, **kwargs: Any):
    """Instantiate an agent adapter plugin by kind name."""
    if kind == "default":
        from agp.plugins.inprocess import DefaultAgentAdapter
        return DefaultAgentAdapter(**kwargs)
    if kind == "codex":
        from agp.plugins.codex import CodexAdapter
        return CodexAdapter(
            cli_command=kwargs.get("cli_command", settings.codex_cli_command),
            idle_poll_seconds=kwargs.get("idle_poll_seconds", settings.codex_idle_poll_seconds),
            idle_after=kwargs.get("idle_after", settings.codex_idle_after),
            idle_timeout_seconds=kwargs.get("idle_timeout_seconds", settings.codex_idle_timeout_seconds),
            session_mode=kwargs.get("session_mode", settings.codex_session_mode),
        )
    if kind == "claude_code":
        from agp.plugins.claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter(
            cli_command=kwargs.get("cli_command", settings.claude_code_cli_command),
            idle_poll_seconds=kwargs.get("idle_poll_seconds", settings.claude_code_idle_poll_seconds),
            idle_after=kwargs.get("idle_after", settings.claude_code_idle_after),
            idle_timeout_seconds=kwargs.get("idle_timeout_seconds", settings.claude_code_idle_timeout_seconds),
            session_mode=kwargs.get("session_mode", settings.claude_code_session_mode),
        )
    raise ValueError(f"unsupported agent adapter kind: {kind}")
