"""``skyops debug`` — developer debugging commands (host, adapter, plugin, runtime, agent, workspace)."""

from __future__ import annotations

import typer

from skyops._plugins import host_app, adapter_app, plugin_app
from skyops._runtime_debug import runtime_debug_app
from skyops._runtime_deploy import register_deploy_command
from skyops._agent_debug import agent_debug_app
from skyops._workspace import workspace_app

register_deploy_command(runtime_debug_app)

debug_app = typer.Typer(help="Developer debugging commands.")
debug_app.add_typer(host_app, name="host")
debug_app.add_typer(adapter_app, name="adapter")
debug_app.add_typer(plugin_app, name="plugin")
debug_app.add_typer(runtime_debug_app, name="runtime")
debug_app.add_typer(agent_debug_app, name="agent")
debug_app.add_typer(workspace_app, name="workspace")
