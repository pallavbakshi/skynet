"""``skyops cp`` — control-plane process operations."""

from __future__ import annotations

import typer

cp_app = typer.Typer(help="Control-plane process operations.")


@cp_app.command("serve")
def cp_serve(
    host: str | None = typer.Option(None, "--host", help="Bind host."),
    port: int | None = typer.Option(None, "--port", help="Bind port."),
) -> None:
    """Run the AGP control plane API server."""
    from agp.cli import serve as agp_serve

    agp_serve(host=host, port=port)
