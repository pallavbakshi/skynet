"""SkyOps CLI — operator interface for the AGP stack."""

from __future__ import annotations

import typer

from skyops._init_cmd import init_app
from skyops._status import status_app
from skyops._config_cmd import config_app
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
from skyops._queue import queue_app, sweep_app

app = typer.Typer(
    name="skyops",
    help="Operator CLI for the AGP stack.",
    no_args_is_help=True,
)

# Phase B: skeleton
app.add_typer(init_app, name="init", invoke_without_command=True)
app.add_typer(config_app, name="config")
app.add_typer(status_app, name="status", invoke_without_command=True)

# Phase C: lifecycle
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
app.add_typer(sweep_app, name="sweep")

if __name__ == "__main__":
    app()
