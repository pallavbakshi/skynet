"""Runtime supervision, terminal host/adapter ABCs, and plugin runner.

``RuntimeClient`` and ``RuntimeIdentity`` are re-exported from
``agp.client`` for backward compatibility.  The canonical import is::

    from agp.client import RuntimeClient, RuntimeIdentity
"""

from __future__ import annotations

# --- Data classes and exceptions ---
from agp.runtime._types import (
    AdapterExecutionFailed,
    ArtifactPayload,
    ExecutionResult,
    InterruptRequested,
    OutputCursor,
    OutputReadResult,
    RecoverableExecutionError,
    SessionHealth,
    TerminalSession,
)

# --- ABCs ---
from agp.runtime._abc import AgentAdapter, TerminalHost

# --- Output utilities ---
from agp.runtime._output import (
    _ANSI_RE,
    _OutputAccumulator,
    _compute_output_delta,
    _strip_ansi,
)

# --- Core supervisor ---
from agp.runtime._supervisor import (
    RuntimeSupervisor,
    _append_runtime_log,
    _failure_snapshot_payloads,
    _make_logging_runtime_client,
    _runtime_log_path,
    register_runtime,
)

# --- Standalone runner ---
from agp.runtime._standalone import (
    StandaloneArtifactRecord,
    StandalonePluginRunner,
    StandaloneRunResult,
    _StandaloneSupervisorContext,
)

# --- Re-exports from SDK package ---
from agp.client._runtime import RuntimeClient, RuntimeIdentity  # noqa: F401

# --- Backward-compatible lazy imports for plugin classes ---

_COMPAT_IMPORTS: dict[str, tuple[str, str]] = {
    "InProcessTerminalHost": ("agp.plugins.inprocess", "InProcessTerminalHost"),
    "DefaultAgentAdapter": ("agp.plugins.inprocess", "DefaultAgentAdapter"),
    "WezTermHost": ("agp.plugins.wezterm", "WezTermHost"),
    "CodexAdapter": ("agp.plugins.codex", "CodexAdapter"),
    "_clean_codex_tui_output": ("agp.plugins.codex", "_clean_codex_tui_output"),
    "build_terminal_host": ("agp.plugins", "build_terminal_host"),
    "build_agent_adapter": ("agp.plugins", "build_agent_adapter"),
}


def __getattr__(name: str):
    if name in _COMPAT_IMPORTS:
        import importlib
        mod_path, attr = _COMPAT_IMPORTS[name]
        mod = importlib.import_module(mod_path)
        val = getattr(mod, attr)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # _types
    "ExecutionResult",
    "ArtifactPayload",
    "TerminalSession",
    "OutputCursor",
    "OutputReadResult",
    "SessionHealth",
    "InterruptRequested",
    "RecoverableExecutionError",
    "AdapterExecutionFailed",
    # _abc
    "TerminalHost",
    "AgentAdapter",
    # _output
    "_OutputAccumulator",
    "_compute_output_delta",
    "_ANSI_RE",
    "_strip_ansi",
    # _supervisor
    "RuntimeSupervisor",
    "_failure_snapshot_payloads",
    "_runtime_log_path",
    "_append_runtime_log",
    "_make_logging_runtime_client",
    "register_runtime",
    # _standalone
    "StandalonePluginRunner",
    "StandaloneRunResult",
    "StandaloneArtifactRecord",
    "_StandaloneSupervisorContext",
    # client re-exports
    "RuntimeClient",
    "RuntimeIdentity",
]
