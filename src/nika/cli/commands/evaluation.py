"""Commands for offline evaluation (metrics, judge, summary)."""

import json
from pathlib import Path

import typer

from nika.config import ENV_RESULT_DIR, resolve_results_root
from nika.run_config.legacy import warn_legacy_operational_env
from nika.run_config.loader import (
    ENV_RUN_CONFIG,
    export_run_config_env,
    load_run_config,
    merge_cli,
    set_run_config,
)
from nika.utils.agent_config import apply_custom_provider_env
from nika.workflows.leaderboard.schema import PRIMARY_METRIC

eval_app = typer.Typer(help="Evaluate a completed agent session.")


@eval_app.command("metrics")
def eval_metrics(
    session_id: str | None = typer.Option(
        None, "--session_id", help="Target session id."
    ),
    result_dir: str | None = typer.Option(
        None,
        "--result_dir",
        help="Results parent directory (default: nika.result_dir in run config).",
    ),
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Path to config/nika.yaml.",
    ),
) -> None:
    """Compute rule-based scores and trace stats on closed session(s); write eval_metrics.json."""
    warn_legacy_operational_env()
    cfg_path = export_run_config_env(run_config)
    cfg = merge_cli(load_run_config(cfg_path), result_dir=result_dir)
    set_run_config(cfg)

    from nika.workflows.eval.session import run_eval_metrics

    try:
        run_eval_metrics(session_id=session_id, result_dir=result_dir)
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@eval_app.command("judge")
def eval_judge(
    judge_provider: str | None = typer.Option(
        None,
        "-p",
        "--provider",
        help="LLM provider for the judge (openai, deepseek, custom).",
    ),
    judge_model: str | None = typer.Option(
        None,
        "-m",
        "--model",
        help="Judge model id.",
    ),
    session_id: str | None = typer.Option(
        None, "--session_id", help="Target session id."
    ),
    result_dir: str | None = typer.Option(
        None,
        "--result_dir",
        help="Results parent directory (default: nika.result_dir in run config).",
    ),
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Path to config/nika.yaml.",
    ),
) -> None:
    """Run LLM-as-judge on closed session(s); write llm_judge.json."""
    warn_legacy_operational_env()
    cfg_path = export_run_config_env(run_config)
    cfg = merge_cli(
        load_run_config(cfg_path),
        result_dir=result_dir,
        judge_provider=judge_provider,
        judge_model=judge_model,
    )
    set_run_config(cfg)
    apply_custom_provider_env(cfg)

    from nika.utils.agent_config import resolve_judge_model, resolve_judge_provider
    from nika.workflows.eval.session import run_llm_judge

    judge_provider = resolve_judge_provider(judge_provider, config=cfg)
    judge_model = resolve_judge_model(judge_model, config=cfg)

    try:
        run_llm_judge(
            judge_provider, judge_model, session_id=session_id, result_dir=result_dir
        )
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@eval_app.command("summary")
def eval_summary(
    output: str | None = typer.Option(
        None,
        "-o",
        "--output",
        help="Output CSV path (default: results/0_summary/evaluation_summary.csv).",
    ),
    problem: list[str] | None = typer.Option(
        None,
        "-p",
        "--problem",
        help="Include only sessions with this root-cause / problem id (repeatable).",
    ),
    env: list[str] | None = typer.Option(
        None,
        "-e",
        "--env",
        help="Include only sessions from this scenario / net env (repeatable).",
    ),
    failure_domain: list[str] | None = typer.Option(
        None,
        "-d",
        "--failure-domain",
        help="Include only sessions in this failure domain (repeatable).",
    ),
    session_id: list[str] | None = typer.Option(
        None,
        "--session_id",
        help="Include only these session ids (repeatable).",
    ),
    agent: list[str] | None = typer.Option(
        None,
        "-a",
        "--agent",
        help="Include only sessions run with this agent type (repeatable).",
    ),
    model: list[str] | None = typer.Option(
        None,
        "--model",
        help="Include only sessions run with this model id (repeatable).",
    ),
    result_dir: str | None = typer.Option(
        None,
        "--result_dir",
        envvar=ENV_RESULT_DIR,
        help="Results parent directory (default: results/). Session output goes to {result_dir}/{session_id}.",
    ),
    report: bool = typer.Option(
        True,
        "--report/--no-report",
        help="Print a visual summary to the terminal after writing the CSV.",
    ),
    group_by: list[str] | None = typer.Option(
        None,
        "-g",
        "--group-by",
        help="Report breakdown dimension: domain, env, problem, size (repeatable).",
    ),
    metric: str = typer.Option(
        PRIMARY_METRIC,
        "--metric",
        help="Metric used to sort report rows and draw bars "
        "(rca_f1, localization_f1, detection_score).",
    ),
    top: int = typer.Option(
        15,
        "--top",
        help="Max rows per report breakdown table; 0 shows all.",
    ),
    json_out: str | None = typer.Option(
        None,
        "--json",
        help="Also write the report aggregates to this JSON path.",
    ),
) -> None:
    """Aggregate finished sessions under the results directory into one CSV file."""
    from nika.workflows.eval.report import GROUP_DIMENSIONS, build_summary_report
    from nika.workflows.eval.summary import collect_eval_summary

    dimensions = tuple(group_by) if group_by else GROUP_DIMENSIONS

    try:
        outcome = collect_eval_summary(
            output_path=output,
            problems=problem,
            envs=env,
            failure_domains=failure_domain,
            session_ids=session_id,
            agent_types=agent,
            models=model,
            results_dir=result_dir,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Wrote summary CSV: {outcome.csv_path}")

    if not report and json_out is None:
        return

    results_root = resolve_results_root(result_dir)
    try:
        summary_report = build_summary_report(
            outcome.session_dirs,
            result_dir=results_root,
            metric=metric,
            dimensions=dimensions,
            # A filtered selection is a subset of the run, so the run-wide
            # expected count would understate the scores.
            n_trials_expected=(len(outcome.session_dirs) if outcome.filtered else None),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    if json_out is not None:
        json_path = Path(json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(
            json.dumps(summary_report.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        typer.echo(f"Wrote summary report JSON: {json_path}")

    if report:
        from nika.workflows.eval.render import render_summary_report

        render_summary_report(
            summary_report,
            metric=metric,
            top=None if top <= 0 else top,
        )


@eval_app.command("clean")
def eval_clean(
    yes: bool = typer.Option(
        False, "-y", "--yes", help="Skip the confirmation prompt."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Delete session files even when running sessions exist.",
    ),
) -> None:
    """Delete session results, runtime session JSON files, and the SQLite session index."""
    from nika.config import SESSIONS_DB, SESSIONS_DIR
    from nika.utils.session_store import SessionStore
    from nika.workflows.eval.clean import run_eval_clean

    running = SessionStore().list_running_sessions()
    if running and not force:
        ids = ", ".join(str(row.get("session_id", "?")) for row in running)
        raise typer.BadParameter(
            f"{len(running)} running session(s) found ({ids}). "
            "Close them with `nika session close` first, or pass --force."
        )

    results_root = resolve_results_root()
    label = (
        f"all files under {results_root}, session files under {SESSIONS_DIR}, "
        f"and the SQLite index at {SESSIONS_DB}"
    )
    if running and force:
        label += f" (including {len(running)} running session file(s))"
    if not yes:
        confirmed = typer.confirm(f"Delete {label}?", default=False)
        if not confirmed:
            raise typer.Abort()

    try:
        report = run_eval_clean(force=force)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(
        f"Removed {report.results_entries_removed} entr{'y' if report.results_entries_removed == 1 else 'ies'} "
        f"under {results_root}, {report.session_files_removed} session file(s) under {SESSIONS_DIR}, "
        f"and cleared the SQLite index at {SESSIONS_DB}."
    )
