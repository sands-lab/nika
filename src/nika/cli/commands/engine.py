"""Command that serves this release's benchmark engine to a NIKA frontend."""

from __future__ import annotations

import os

import typer

engine_app = typer.Typer(help="Benchmark engine service (started by the frontend).")

ENV_ENGINE_TOKEN = "NIKA_ENGINE_TOKEN"


@engine_app.command("serve")
def engine_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(..., "--port", "-p", help="TCP port."),
    parent_pid: int | None = typer.Option(
        None, "--parent-pid", help="Exit when this frontend process ends."
    ),
) -> None:
    """Serve engine API version 1; the run token is read from NIKA_ENGINE_TOKEN."""
    token = os.environ.get(ENV_ENGINE_TOKEN, "")
    if not token:
        typer.echo(f"{ENV_ENGINE_TOKEN} is not set.", err=True)
        raise typer.Exit(1)
    from nika.benchmark_engine.server import serve_engine

    serve_engine(host=host, port=port, token=token, parent_pid=parent_pid)
