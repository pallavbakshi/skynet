"""SkyOps CLI — operator interface for the AGP stack."""

from __future__ import annotations

import typer

from skyops._init_cmd import init_app
from skyops._control_plane import cp_app
from skyops._status import status_app
from skyops._config_cmd import config_app
from skyops._infra import deps_app
from skyops._lifecycle import lifecycle_app
from skyops._db import db_app
from skyops._health import health_app
from skyops._dispatch import dispatch_app
from skyops._monitor import monitor_app, logs_app
from skyops._backup import backup_app
from skyops._security import security_app
from skyops._upgrade import upgrade_app
from skyops._drill import drill_app
from skyops._plugins import host_app, adapter_app, plugin_app
from skyops._queue import queue_app, job_app, sweep_app
from skyops._validate import validate_app
from skyops._runtime_debug import runtime_debug_app
from skyops._runtime_deploy import register_deploy_command
from skyops._agent_debug import agent_debug_app
from skyops._workspace import workspace_app

register_deploy_command(runtime_debug_app)

app = typer.Typer(
    name="skyops",
    help="Operator CLI for the AGP stack.",
    no_args_is_help=True,
)

# Phase B: skeleton
app.add_typer(init_app, name="init", invoke_without_command=True)
app.add_typer(config_app, name="config")
app.add_typer(status_app, name="status", invoke_without_command=True)
app.add_typer(deps_app, name="deps")

# Phase C: lifecycle
app.add_typer(cp_app, name="cp")
app.add_typer(lifecycle_app)
app.add_typer(db_app, name="db")
app.add_typer(health_app, name="health", invoke_without_command=True)

# Phase D: operator commands
app.add_typer(dispatch_app)
app.add_typer(monitor_app)
app.add_typer(logs_app, name="logs")
app.add_typer(backup_app, name="backup")
app.add_typer(security_app, name="secrets")
app.add_typer(upgrade_app, name="upgrade")
app.add_typer(drill_app, name="drill")
app.add_typer(host_app, name="host")
app.add_typer(adapter_app, name="adapter")
app.add_typer(plugin_app, name="plugin")
app.add_typer(queue_app, name="queue")
app.add_typer(job_app, name="job")
app.add_typer(sweep_app, name="sweep")
app.add_typer(validate_app)
app.add_typer(runtime_debug_app, name="runtime")
app.add_typer(agent_debug_app, name="agent")
app.add_typer(workspace_app, name="workspace")

if __name__ == "__main__":
    app()
