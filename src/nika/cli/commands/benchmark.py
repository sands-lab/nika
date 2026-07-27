"""Benchmark runner: env → fault → agent → close + metrics."""

from pathlib import Path

import typer

from nika.config import ENV_RESULT_DIR
from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.utils.agent_config import (
    ENV_AGENT_TYPE,
    ENV_LLM_PROVIDER,
    ENV_MAX_STEPS,
    ENV_MODEL,
)
from nika.workflows.benchmark.release import (
    ReleaseError,
    list_releases,
    load_release,
    normalize_split,
    parse_release_ref,
    preflight_release,
    resolve_release_dir,
)
from nika.workflows.benchmark.run import (
    run_benchmark_from_release,
    run_benchmark_from_yaml,
    run_single_case,
    validate_inject_params,
)

benchmark_app = typer.Typer(
    help="Run curated benchmark cases (env → fault → agent → close + metrics)."
)


def _parse_set_options(raw_items: list[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for raw in raw_items or []:
        if "=" not in raw:
            raise typer.BadParameter(f"Invalid --set value {raw!r}. Use key=value.")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(
                f"Invalid --set value {raw!r}. Key cannot be empty."
            )
        overrides[key] = value.strip()
    return overrides


def _exit_release_error(exc: Exception) -> None:
    typer.secho(str(exc), fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1) from exc


def _default_split_for_version(version: str) -> str:
    """Read ``default_split_for_release`` from ``RELEASE.yaml`` (fallback: test)."""
    import yaml

    manifest_path = resolve_release_dir(version) / "RELEASE.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return normalize_split(
        str(data.get("default_split_for_release") or "test"),
        default="test",
    )


@benchmark_app.command("releases")
def benchmark_releases() -> None:
    """List frozen releases and run preflight verification for each."""
    versions = list_releases()
    if not versions:
        typer.echo("No releases found under benchmark/releases/")
        return
    failures = 0
    for version in versions:
        try:
            split = _default_split_for_version(version)
            release = load_release(version, split=split, verify_digest=True)
            preflight_release(release, check_images=True)
            typer.echo(
                f"OK {release.ref}  cases={release.case_count}  "
                f"n_trials={release.n_trials}  "
                f"digest={release.benchmark_digest}"
            )
        except ReleaseError as exc:
            failures += 1
            typer.secho(f"FAIL {version}: {exc}", fg=typer.colors.RED, err=True)
    if failures:
        raise typer.Exit(code=1)


@benchmark_app.command("run")
def benchmark_run(
    scenario: str | None = typer.Argument(
        default=None,
        metavar="SCENARIO",
        help="Scenario id for a single case (omit for --release / --config batch mode).",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Ad-hoc benchmark YAML path (batch mode). Mutually exclusive with --release.",
    ),
    release: str | None = typer.Option(
        None,
        "--release",
        "-d",
        help=(
            "Frozen release version or ref (e.g. 0.1.0, nika@0.1, nika-bench@0.1.0). "
            "Uses RELEASE.yaml default_split_for_release. Mutually exclusive with --config."
        ),
    ),
    problem: str | None = typer.Option(
        None,
        "--problem",
        help="Problem id for a single case (required with SCENARIO).",
    ),
    size: str | None = typer.Option(
        None,
        "-s",
        "--size",
        help="Topology size s, m, or l (required only for scalable scenarios).",
    ),
    sets: list[str] | None = typer.Option(
        None,
        "--set",
        help="Inject parameters as key=value (required in single-case mode).",
    ),
    agent_type: str | None = typer.Option(
        None,
        "-a",
        "--agent",
        envvar=ENV_AGENT_TYPE,
        help="Agent implementation (required unless NIKA_AGENT_TYPE is in .env).",
    ),
    llm_provider: str | None = typer.Option(
        None,
        "-p",
        "--provider",
        envvar=ENV_LLM_PROVIDER,
        help="LLM provider for byo.langgraph only: openai, ollama, deepseek, custom.",
    ),
    model: str | None = typer.Option(
        None,
        "-m",
        "--model",
        envvar=ENV_MODEL,
        help="Model id (required unless agent-specific NIKA_*_MODEL or NIKA_MODEL is in .env).",
    ),
    max_steps: int | None = typer.Option(
        None,
        "-n",
        "--max-steps",
        envvar=ENV_MAX_STEPS,
        help="Max steps per phase (required unless NIKA_MAX_STEPS is in .env; byo.langgraph, byo.mcp_agent, byo.autogen, community.sade).",
    ),
    batch_size: int = typer.Option(
        1,
        "--batch-size",
        help=(
            "Batch mode: number of cases/trials to run simultaneously per batch. "
            "Rows are chunked into groups of this size; each group runs fully in "
            "parallel before the next group starts (default: 1)."
        ),
    ),
    result_dir: str | None = typer.Option(
        None,
        "--result_dir",
        envvar=ENV_RESULT_DIR,
        help=(
            "Results parent directory (default: results/). "
            "Release/batch run: this directory is one run "
            "({result_dir}/run.json + trials/). "
            "Single-case CLI: session output goes to {result_dir}/{session_id}."
        ),
    ),
    session_tag: str | None = typer.Option(
        None,
        "--session-tag",
        help="Optional tag embedded in each session id (YYYYMMDD-HHMMSS-tag-{hex}).",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--no-resume",
        help=(
            "Batch mode: scan all rows, skip completed cases/trials, "
            "run the rest (default). Use --no-resume to re-run every case."
        ),
    ),
    case_timeout: int | None = typer.Option(
        None,
        "--case-timeout",
        envvar="NIKA_CASE_TIMEOUT",
        help=(
            "Batch mode: hard per-case watchdog in seconds. "
            "Release mode defaults to the release default (2400 for 0.1.0) when omitted. "
            "Ad-hoc --config mode defaults to 0 (disabled)."
        ),
    ),
    continue_on_error: bool = typer.Option(
        False,
        "--continue-on-error/--abort-on-error",
        envvar="NIKA_CONTINUE_ON_ERROR",
        help=(
            "Batch mode: keep running past failed cases and summarize them "
            "at the end (default: abort on the first failure)."
        ),
    ),
    retry_passes: int = typer.Option(
        0,
        "--retry-passes",
        envvar="NIKA_RETRY_PASSES",
        help=(
            "Batch mode: after the first pass, automatically re-scan and "
            "retry failed/incomplete cases up to this many extra passes (implies "
            "--continue-on-error). Stops early when a pass completes no new "
            "case. Release/batch runs only retry incomplete trials; agent_failed trials are kept."
        ),
    ),
) -> None:
    """Run a frozen release, an ad-hoc YAML batch, or a single case.

    Batch mode requires explicit ``--config`` or ``--release`` (no bare default).
    """
    if scenario is not None and (config is not None or release is not None):
        raise typer.BadParameter(
            "Use either SCENARIO (single-case), --config (ad-hoc YAML), or "
            "--release (frozen suite), not a combination."
        )
    if config is not None and release is not None:
        raise typer.BadParameter("Use either --config or --release, not both.")

    single_mode = scenario is not None

    if single_mode:
        if batch_size != 1:
            raise typer.BadParameter(
                "--batch-size applies to batch mode only; omit it for a single case."
            )
        if case_timeout:
            raise typer.BadParameter(
                "--case-timeout applies to batch mode only; omit it for a single case."
            )
        if continue_on_error:
            raise typer.BadParameter(
                "--continue-on-error applies to batch mode only; omit it for a single case."
            )
        if retry_passes:
            raise typer.BadParameter(
                "--retry-passes applies to batch mode only; omit it for a single case."
            )
        if not problem:
            raise typer.BadParameter("--problem is required when SCENARIO is given.")
        if scenario_requires_topo_size(scenario) and not size:
            raise typer.BadParameter(
                f"Scenario '{scenario}' requires -s/--size (s, m, or l)."
            )
        if not scenario_requires_topo_size(scenario) and size is not None:
            raise typer.BadParameter(
                f"Scenario '{scenario}' does not use sizes; omit -s/--size."
            )
        topo = size or ""
        inject_params = _parse_set_options(sets)
        if not inject_params:
            raise typer.BadParameter(
                "Single-case mode requires complete inject parameters via --set key=value. "
                "Use --release for a frozen suite, or --config for ad-hoc YAML."
            )
        try:
            validate_inject_params(problem, scenario, topo, inject_params)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        run_single_case(
            problem=problem,
            scenario=scenario,
            topo_size=topo,
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            inject_params=inject_params,
            result_dir=result_dir,
            session_tag=session_tag,
        )
        return

    if problem is not None:
        raise typer.BadParameter(
            "--problem without SCENARIO is invalid; pass SCENARIO or use "
            "--release / --config batch mode."
        )

    if config is None and release is None:
        raise typer.BadParameter(
            "Pass --config PATH or --release REF "
            "(e.g. --release 0.1.0). There is no default benchmark suite."
        )

    if config is not None:
        run_benchmark_from_yaml(
            benchmark_file=str(config),
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            batch_size=batch_size,
            result_dir=result_dir,
            resume=resume,
            session_tag=session_tag,
            case_timeout=case_timeout if case_timeout is not None else 0,
            continue_on_error=continue_on_error,
            retry_passes=retry_passes,
        )
        return

    # Release path (split from RELEASE.yaml default_split_for_release).
    try:
        _, version = parse_release_ref(release)
        resolved_split = _default_split_for_version(version)
        run_benchmark_from_release(
            release_ref=release,
            split=resolved_split,
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            batch_size=batch_size,
            result_dir=result_dir,
            resume=resume,
            session_tag=session_tag,
            case_timeout=case_timeout,
            continue_on_error=continue_on_error,
            retry_passes=retry_passes,
        )
    except (ReleaseError, ValueError) as exc:
        _exit_release_error(exc)
