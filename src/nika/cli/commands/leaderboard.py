"""Leaderboard submission: template and pack+validate+submit."""

from pathlib import Path
from typing import Optional

import typer

from nika.config import ENV_RESULT_DIR
from nika.workflows.leaderboard.hf_remote import DEFAULT_TRAJECTORIES_REPO
from nika.workflows.leaderboard.remote import DEFAULT_LEADERBOARD_REPO

leaderboard_app = typer.Typer(
    help="Pack, validate, and submit leaderboard entries from official release runs."
)


@leaderboard_app.command("template")
def leaderboard_template(
    out: Path = typer.Option(
        Path("submission"),
        "-o",
        "--out",
        help="Directory for empty metadata.yaml + README.md templates.",
    ),
) -> None:
    """Write empty metadata.yaml + README.md templates for a submission."""
    from nika.workflows.leaderboard.meta_input import write_submission_templates

    path = write_submission_templates(out)
    typer.echo(f"Wrote submission templates under: {path}")
    typer.echo("Edit metadata.yaml and README.md, then:")
    typer.echo(
        f"  nika leaderboard submit --result_dir … --submission {path}"
    )


@leaderboard_app.command("submit")
def leaderboard_submit(
    result_dir: Optional[str] = typer.Option(
        None,
        "--result_dir",
        envvar=ENV_RESULT_DIR,
        help="Finished official release-run directory (contains run.json + trials/).",
    ),
    submission: Path = typer.Option(
        ...,
        "--submission",
        help="Directory with edited metadata.yaml + README.md "
        "(see `nika leaderboard template`).",
    ),
    out: Optional[Path] = typer.Option(
        None,
        "--out",
        help="Output scores package directory "
        "(default: {result_dir}/{YYYYMMDD}_{slug}/). "
        "Trajectories land in a sibling {name}_trajectories/ directory.",
    ),
    repo: str = typer.Option(
        DEFAULT_LEADERBOARD_REPO,
        "--repo",
        help="GitHub owner/name of the leaderboard archive repository.",
    ),
    traj_repo: str = typer.Option(
        DEFAULT_TRAJECTORIES_REPO,
        "--traj-repo",
        help="Hugging Face dataset repo for trajectories (owner/name).",
    ),
    draft: bool = typer.Option(
        False,
        "--draft",
        help="Open the GitHub pull request as a draft.",
    ),
    skip_validate: bool = typer.Option(
        False,
        "--skip-validate",
        help="Skip local validation before opening PRs.",
    ),
    skip_github: bool = typer.Option(
        False,
        "--skip-github",
        help="Do not open a GitHub scores PR.",
    ),
    skip_trajectories: bool = typer.Option(
        False,
        "--skip-trajectories",
        help="Do not open a Hugging Face trajectories PR.",
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help="GitHub pull request title (default: Add submission <package>).",
    ),
    body: Optional[str] = typer.Option(
        None,
        "--body",
        help="GitHub pull request body (default: short auto-generated summary).",
    ),
) -> None:
    """Pack, validate, and open GitHub (scores) + HF (trajectories) PRs.

    Pack or validate failures print an error and exit before any remote submit.
    """
    from nika.workflows.leaderboard.submit import (
        LeaderboardSubmitError,
        submit_leaderboard_package,
    )
    from nika.workflows.leaderboard.validate import LeaderboardValidateError

    if not result_dir:
        raise typer.BadParameter(
            "--result_dir is required (or set nika.result_dir in config/nika.yaml)"
        )

    try:
        result = submit_leaderboard_package(
            result_dir,
            submission_dir=submission,
            out_dir=out,
            repo=repo,
            draft=draft,
            skip_validate=skip_validate,
            title=title,
            body=body,
            skip_github=skip_github,
            skip_trajectories=skip_trajectories,
            traj_repo=traj_repo,
        )
    except (LeaderboardSubmitError, LeaderboardValidateError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Packed scores package: {result.package_dir}")
    if result.trajectories_dir is not None:
        typer.echo(f"Packed trajectories package: {result.trajectories_dir}")

    if result.pr_url:
        typer.echo(f"Pushed {result.remote_path} on branch {result.branch}")
        if result.used_fork:
            typer.echo(f"Used fork head: {result.head}")
        typer.echo(f"Opened GitHub pull request: {result.pr_url}")
    if result.trajectories_pr_url:
        typer.echo(
            f"Opened HF trajectories PR: {result.trajectories_pr_url} "
            f"({result.trajectories_remote_path})"
        )
