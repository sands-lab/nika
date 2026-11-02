"""Machine-readable single-episode entrypoint for external frameworks."""

from pathlib import Path

import typer

rollout_app = typer.Typer(help="Run immutable task attempts for external frameworks.")


@rollout_app.command("run")
def rollout_run(
    request: Path = typer.Option(..., exists=True), output_dir: Path = typer.Option(...)
):
    """Run a JSON RolloutRequest and write a RolloutResult plus native artifacts."""
    from nika.rollout import RolloutRequest, run_rollout

    result = run_rollout(
        RolloutRequest.model_validate_json(request.read_text()), output_dir
    )
    typer.echo(result.model_dump_json())
    if result.reward is None:
        raise typer.Exit(1)
