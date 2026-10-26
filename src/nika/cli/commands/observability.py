"""CLI: toggle monitoring of lab resources and benchmark progress."""

from __future__ import annotations

import typer

observability_app = typer.Typer(
    help="Record lab CPU and memory per session and benchmark progress; view them in Grafana.",
)


@observability_app.command("on")
def observability_on() -> None:
    """Start cAdvisor, Prometheus, and Grafana; later labs and runs report automatically."""
    from nika.metrics.stack import GRAFANA_URL, PROMETHEUS_URL, observability_on

    try:
        observability_on()
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("Observability on.")
    typer.echo(f"grafana: {GRAFANA_URL}")
    typer.echo(f"prometheus: {PROMETHEUS_URL}")


@observability_app.command("off")
def observability_off(
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Also delete the recorded Prometheus history and Grafana state.",
    ),
) -> None:
    """Stop the monitoring stack and the session exporters."""
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
        typer.echo(f"grafana: {state['grafana']}")
        typer.echo(f"prometheus: {state['prometheus']}")
        if state["remote_write"]:
            typer.echo(f"remote write: {state['remote_write']}")
    typer.echo(
        f"exporter (this checkout): {'running' if state['exporter_running'] else 'stopped'}"
    )
    typer.echo(f"exporters (all checkouts): {state['exporters']}")
