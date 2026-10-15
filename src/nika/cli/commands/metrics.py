"""CLI: opt-in Prometheus monitoring of lab resource usage."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

metrics_app = typer.Typer(
    help="Monitor lab CPU and memory usage per session with Prometheus.",
)

_COMPOSE_FILE = (
    Path(__file__).resolve().parents[2] / "metrics" / "stack" / "docker-compose.yml"
)


def _compose(*args: str) -> None:
    result = subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), *args], check=False
    )
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


@metrics_app.command("up")
def metrics_up() -> None:
    """Start cAdvisor and Prometheus (http://127.0.0.1:9090)."""
    _compose("up", "-d")
    typer.echo(
        "Prometheus: http://127.0.0.1:9090. "
        "Run `nika metrics serve` to attribute containers to sessions."
    )


@metrics_app.command("down")
def metrics_down(
    volumes: bool = typer.Option(
        False, "--volumes", help="Also delete the stored Prometheus data."
    ),
) -> None:
    """Stop cAdvisor and Prometheus."""
    _compose("down", *(["--volumes"] if volumes else []))


@metrics_app.command("serve")
def metrics_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(9464, "--port", help="Port of the /metrics endpoint."),
) -> None:
    """Export which lab containers belong to which running session."""
    from nika.metrics.exporter import serve_metrics

    typer.echo(f"Serving NIKA session metrics on http://{host}:{port}/metrics")
    serve_metrics(host=host, port=port)
