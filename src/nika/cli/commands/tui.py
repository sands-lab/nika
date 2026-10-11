"""Interactive NIKA shell with slash commands and guided forms."""

import sys

import typer

from nika.run_config.loader import ENV_RUN_CONFIG

tui_app = typer.Typer(help="Interactive shell with slash commands.")


@tui_app.command("tui")
def tui_command(
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Initial agent profile (default: config/nika.yaml).",
    ),
) -> None:
    """Open the interactive NIKA shell. Every action maps to a regular CLI command."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo("nika tui needs an interactive terminal.", err=True)
        raise typer.Exit(code=2)
    from nika.tui.app import run_tui

    run_tui(run_config)
