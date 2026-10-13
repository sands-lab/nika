"""CLI: local web UI for browsing session trajectories and scores."""

from __future__ import annotations

import typer

inspect_app = typer.Typer(
    help="Browse finished and running session trajectories in a local web UI.",
)


@inspect_app.command("inspect")
def inspect_command(
    result_dir: str | None = typer.Option(
        None,
        "--result-dir",
        "--result_dir",
        help="Results root to browse (default: config/nika.yaml or results/).",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address: 127.0.0.1 (default), localhost, ::1, 0.0.0.0, or ::.",
    ),
    port: int = typer.Option(
        7580,
        "--port",
        help="Port (0 = OS-assigned). Default 7580; falls back if busy.",
    ),
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Do not open a browser (always implied under SSH).",
    ),
) -> None:
    """Start a local web UI for session trajectories, scores, and timelines."""
    from nika.inspect.serve import serve_inspect

    try:
        serve_inspect(
            result_dir=result_dir,
            host=host,
            port=port,
            open_browser=not no_open,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
