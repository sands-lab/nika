"""Commands for benchmark engine images and containers (contract B)."""

from __future__ import annotations

import os

import typer

engine_app = typer.Typer(help="Benchmark engines of published releases.")


@engine_app.command("build")
def engine_build(
    ref: str = typer.Argument(..., help="Git tag or commit to build, e.g. v0.2.0."),
) -> None:
    """Build the benchmark engine image of a release tag or commit."""
    from nika.benchmark_engine.container import EngineContainerError, build_engine_image

    try:
        image = build_engine_image(ref)
    except EngineContainerError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(image)


# Started by an engine image's entrypoint, never by users.
@engine_app.command("serve", hidden=True)
def engine_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8710, "--port", "-p", help="TCP port."),
    parent_pid: int | None = typer.Option(
        None, "--parent-pid", help="Exit when this frontend process ends."
    ),
) -> None:
    """Serve this NIKA's benchmark engine; the frontend starts it in a container."""
    from nika.benchmark_engine.container import ENV_ENGINE_TOKEN
    from nika.benchmark_engine.server import serve_engine

    token = os.environ.get(ENV_ENGINE_TOKEN, "")
    if not token:
        typer.secho(f"{ENV_ENGINE_TOKEN} is not set.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    serve_engine(host=host, port=port, token=token, parent_pid=parent_pid)
