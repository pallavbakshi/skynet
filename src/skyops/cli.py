"""SkyOps CLI — operator interface for the AGP stack."""

from __future__ import annotations

import typer

from skyops._init_cmd import init_app
from skyops._status import status_app
from skyops._config_cmd import config_app
from skyops._lifecycle import lifecycle_app
from skyops._db import db_app
from skyops._health import health_app

app = typer.Typer(
    name="skyops",
    help="Operator CLI for the AGP stack.",
    no_args_is_help=True,
)

app.add_typer(init_app, name="init", invoke_without_command=True)
app.add_typer(config_app, name="config")
app.add_typer(status_app, name="status", invoke_without_command=True)
app.add_typer(lifecycle_app)
app.add_typer(db_app, name="db")
app.add_typer(health_app, name="health", invoke_without_command=True)

if __name__ == "__main__":
    app()
