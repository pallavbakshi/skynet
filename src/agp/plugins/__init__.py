"""Plugin registry for terminal hosts and agent adapters."""
from __future__ import annotations
from typing import Any
from agp.config import settings


def build_terminal_host(kind: str, **kwargs: Any):
    """Instantiate a terminal host plugin by kind name."""
    if kind == "inprocess":
        from agp.plugins.inprocess import InProcessTerminalHost
        return InProcessTerminalHost()
    if kind == "wezterm":
        from agp.plugins.wezterm import WezTermHost
        kwargs.setdefault("scrollback_lines", settings.wezterm_scrollback_lines)
        kwargs.setdefault("checkpoint_dir", settings.output_checkpoint_dir)
        kwargs.setdefault("default_cwd", settings.wezterm_default_cwd)
        return WezTermHost(**kwargs)
    if kind == "tmux":
        from agp.plugins.tmux import TmuxHost
        kwargs.pop("workspace", None)  # WezTerm-specific, not used by tmux
        kwargs.setdefault("scrollback_lines", settings.wezterm_scrollback_lines)
        kwargs.setdefault("checkpoint_dir", settings.output_checkpoint_dir)
        kwargs.setdefault("default_cwd", getattr(settings, "tmux_default_cwd", "") or "")
        kwargs.setdefault("session_prefix", getattr(settings, "tmux_session_prefix", "agp"))
        return TmuxHost(**kwargs)
    raise ValueError(f"unsupported terminal host kind: {kind}")


def build_agent_adapter(kind: str, **kwargs: Any):
    """Instantiate an agent adapter plugin by kind name."""
    if kind == "default":
        from agp.plugins.inprocess import DefaultAgentAdapter
        return DefaultAgentAdapter(**kwargs)
    if kind == "codex":
        from agp.plugins.codex import CodexAdapter
        return CodexAdapter(
            begin_marker=kwargs.get("begin_marker", settings.codex_begin_marker),
            result_marker=kwargs.get("result_marker", settings.codex_result_marker),
            max_polls=kwargs.get("max_polls", settings.codex_max_polls),
            poll_interval_seconds=kwargs.get("poll_interval_seconds", settings.codex_poll_interval_seconds),
            bootstrap_settle_seconds=kwargs.get("bootstrap_settle_seconds", settings.codex_bootstrap_settle_seconds),
            idle_timeout_polls=kwargs.get("idle_timeout_polls", settings.codex_idle_timeout_polls),
            health_check_interval_polls=kwargs.get("health_check_interval_polls", settings.codex_health_check_interval_polls),
            cli_command=kwargs.get("cli_command", settings.codex_cli_command),
            tui_mode=kwargs.get("tui_mode", settings.codex_tui_mode),
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
            bootstrap_settle_seconds=kwargs.get("bootstrap_settle_seconds", settings.claude_code_bootstrap_settle_seconds),
        )
    raise ValueError(f"unsupported agent adapter kind: {kind}")
