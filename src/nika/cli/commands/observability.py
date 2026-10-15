"""CLI: toggle Prometheus monitoring of lab resource usage."""

from __future__ import annotations

import typer

observability_app = typer.Typer(
    help="Record lab CPU and memory per session in Prometheus.",
)


@observability_app.command("on")
def observability_on() -> None:
    """Start cAdvisor and Prometheus; later labs report their usage automatically."""
    from nika.metrics.stack import PROMETHEUS_URL, observability_on

    try:
        observability_on()
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Observability on. Prometheus: {PROMETHEUS_URL}")


@observability_app.command("off")
def observability_off(
    purge: bool = typer.Option(
        False, "--purge", help="Also delete the recorded Prometheus history."
    ),
) -> None:
    """Stop cAdvisor, Prometheus, and the session exporters."""
    from nika.metrics.stack import observability_off

    try:
        observability_off(purge=purge)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("Observability off.")


@observability_app.command("status")
def observability_status() -> None:
    """Show whether monitoring is on and this checkout's exporter is running."""
    from nika.metrics.stack import status

    state = status()
    typer.echo(f"observability: {'on' if state['on'] else 'off'}")
    if state["on"]:
        typer.echo(f"prometheus: {state['prometheus']}")
    typer.echo(
        f"exporter (this checkout): {'running' if state['exporter_running'] else 'stopped'}"
    )
    typer.echo(f"exporters (all checkouts): {state['exporters']}")
