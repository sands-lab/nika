"""Leaderboard submission pack, validate, and GitHub submit."""

from pathlib import Path
from typing import Optional

import typer

from nika.config import ENV_RESULT_DIR
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
    typer.echo(f"  nika leaderboard pack --result_dir … --submission {path}")


@leaderboard_app.command("pack")
def leaderboard_pack(
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
        help="Output package directory (default: {result_dir}/{YYYYMMDD}_{slug}/).",
    ),
) -> None:
    """Create a leaderboard submission package from a completed release run."""
    from nika.workflows.leaderboard.meta_input import MetaInputError
    from nika.workflows.leaderboard.pack import (
        LeaderboardPackError,
        pack_leaderboard_submission,
    )

    if not result_dir:
        raise typer.BadParameter("--result_dir is required (or set NIKA_RESULT_DIR)")

    try:
        package = pack_leaderboard_submission(
            result_dir,
            submission_dir=submission,
            out_dir=out,
        )
    except (LeaderboardPackError, MetaInputError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Wrote leaderboard submission package: {package}")


@leaderboard_app.command("validate")
def leaderboard_validate(
    submission_dir: Path = typer.Argument(
        ...,
        help="Leaderboard submission package directory.",
    ),
    source_result_dir: Optional[str] = typer.Option(
        None,
        "--source-result-dir",
        help="Optional original release-run directory to re-check run.json sha256.",
    ),
) -> None:
    """Validate a leaderboard submission package locally."""
    from nika.workflows.leaderboard.validate import validate_leaderboard_submission

    report = validate_leaderboard_submission(
        submission_dir,
        source_result_dir=source_result_dir,
    )
    for warning in report.warnings:
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
    if report.ok:
        typer.echo("Leaderboard submission validation passed.")
        return
    for error in report.errors:
        typer.secho(f"error: {error}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@leaderboard_app.command("submit")
def leaderboard_submit(
    package_dir: Path = typer.Argument(
        ...,
        help="Packed submission directory ({YYYYMMDD}_{slug}/).",
    ),
    repo: str = typer.Option(
        DEFAULT_LEADERBOARD_REPO,
        "--repo",
        help="GitHub owner/name of the leaderboard archive repository.",
    ),
    draft: bool = typer.Option(
        False,
        "--draft",
        help="Open the pull request as a draft.",
    ),
    skip_validate: bool = typer.Option(
        False,
        "--skip-validate",
        help="Skip local validation before opening the PR.",
    ),
    title: Optional[str] = typer.Option(
        None,
        "--title",
        help="Pull request title (default: Add submission <package>).",
    ),
    body: Optional[str] = typer.Option(
        None,
        "--body",
        help="Pull request body (default: short auto-generated summary).",
    ),
) -> None:
    """Validate a package and open a PR on the leaderboard GitHub repository."""
    from nika.workflows.leaderboard.submit import (
        LeaderboardSubmitError,
        submit_leaderboard_package,
    )
    from nika.workflows.leaderboard.validate import LeaderboardValidateError

    try:
        result = submit_leaderboard_package(
            package_dir,
            repo=repo,
            draft=draft,
            skip_validate=skip_validate,
            title=title,
            body=body,
        )
    except (LeaderboardSubmitError, LeaderboardValidateError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Pushed {result.remote_path} on branch {result.branch}")
    if result.used_fork:
        typer.echo(f"Used fork head: {result.head}")
    typer.echo(f"Opened pull request: {result.pr_url}")
