"""Runtime supervision, terminal host/adapter ABCs, and plugin runner.

``RuntimeClient`` and ``RuntimeIdentity`` are re-exported from
``agp.client`` for backward compatibility.  The canonical import is::

    from agp.client import RuntimeClient, RuntimeIdentity
"""

from __future__ import annotations

# --- Re-exports from SDK package ---
from agp.client._runtime import RuntimeClient, RuntimeIdentity

# --- ABCs ---
from agp.runtime._abc import AgentAdapter, TerminalHost

# --- Output utilities ---
from agp.runtime._output import (
    _ANSI_RE,
    _compute_output_delta,
    _OutputAccumulator,
    _strip_ansi,
)

# --- Standalone runner ---
from agp.runtime._standalone import (
    StandaloneArtifactRecord,
    StandalonePluginRunner,
    StandaloneRunResult,
    _StandaloneSupervisorContext,
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

# --- Data classes and exceptions ---
from agp.runtime._types import (
    AdapterExecutionFailed,
    ArtifactPayload,
    AuthFailure,
    BootstrapFailure,
    ExecutionResult,
    ExecutionTimeout,
    InterruptRequested,
    OutputCursor,
    OutputReadResult,
    PaneDied,
    RecoverableExecutionError,
    SessionHealth,
    StableButIndeterminate,
    TerminalSession,
)

# --- Backward-compatible lazy imports for plugin classes ---

_COMPAT_IMPORTS: dict[str, tuple[str, str]] = {
    "InProcessTerminalHost": ("agp.plugins.inprocess", "InProcessTerminalHost"),
    "DefaultAgentAdapter": ("agp.plugins.inprocess", "DefaultAgentAdapter"),
    "SmallopsTerminalHost": ("agp.plugins._smallops_host", "SmallopsTerminalHost"),
    "CodexAdapter": ("agp.plugins.codex", "CodexAdapter"),
    "ClaudeCodeAdapter": ("agp.plugins.claude_code", "ClaudeCodeAdapter"),
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
    "_ANSI_RE",
    "AdapterExecutionFailed",
    "AgentAdapter",
    "ArtifactPayload",
    "AuthFailure",
    "BootstrapFailure",
    # _types
    "ExecutionResult",
    "ExecutionTimeout",
    "InterruptRequested",
    "OutputCursor",
    "OutputReadResult",
    "PaneDied",
    "RecoverableExecutionError",
    # client re-exports
    "RuntimeClient",
    "RuntimeIdentity",
    # _supervisor
    "RuntimeSupervisor",
    "SessionHealth",
    "StableButIndeterminate",
    "StandaloneArtifactRecord",
    # _standalone
    "StandalonePluginRunner",
    "StandaloneRunResult",
    # _abc
    "TerminalHost",
    "TerminalSession",
    # _output
    "_OutputAccumulator",
    "_StandaloneSupervisorContext",
    "_append_runtime_log",
    "_compute_output_delta",
    "_failure_snapshot_payloads",
    "_make_logging_runtime_client",
    "_runtime_log_path",
    "_strip_ansi",
    "register_runtime",
]
