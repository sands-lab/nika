"""Benchmark runner: env → fault → agent → close + metrics."""

from pathlib import Path

import typer

from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.run_config.loader import (
    ENV_RUN_CONFIG,
    load_run_config,
    merge_cli,
    set_run_config,
)
from nika.run_config.legacy import warn_legacy_operational_env
from nika.utils.agent_config import apply_custom_provider_env
from nika.workflows.benchmark.release import (
    ReleaseError,
    freeze_split_release,
    is_deprecated_release,
    list_releases,
    load_release,
    normalize_split,
    parse_release_ref,
    preflight_release,
    resolve_release_dir,
)
from nika.workflows.benchmark.run import (
    default_benchmark_yaml_path,
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
        if is_deprecated_release(version):
            typer.secho(
                f"DEPRECATED {version}: retained for provenance; not runnable",
                fg=typer.colors.YELLOW,
            )
            continue
        try:
            split = _default_split_for_version(version)
            release = load_release(version, split=split)
            preflight_release(release, check_images=True)
            typer.echo(
                f"OK {release.ref}  cases={release.case_count}  "
                f"n_trials={release.n_trials}"
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
        help="Agent implementation (default: agent.type in run config).",
    ),
    llm_provider: str | None = typer.Option(
        None,
        "-p",
        "--provider",
        help="LLM provider: openai, anthropic, deepseek, custom.",
    ),
    model: str | None = typer.Option(
        None,
        "-m",
        "--model",
        help="Model id (default: agent.model / agent.models.* in run config).",
    ),
    max_steps: int | None = typer.Option(
        None,
        "-n",
        "--max-steps",
        help="Max steps per phase (default: agent.max_steps in run config).",
    ),
    access_role: str | None = typer.Option(
        None, "--role", help="Diagnosis access role (default: agent.access.role)."
    ),
    run_config: str | None = typer.Option(
        None,
        "--run-config",
        envvar=ENV_RUN_CONFIG,
        help="Path to config/nika.yaml (default: config/nika.yaml).",
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        help=(
            "Batch mode: number of cases/trials to run simultaneously per batch "
            "(default: benchmark.batch_size in run config)."
        ),
    ),
    result_dir: str | None = typer.Option(
        None,
        "--result_dir",
        help=(
            "Results parent directory (default: nika.result_dir in run config). "
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
    resume: bool | None = typer.Option(
        None,
        "--resume/--no-resume",
        help=(
            "Batch mode: scan all rows, skip completed cases/trials, "
            "run the rest (default from run config). Use --no-resume to re-run every case."
        ),
    ),
    case_timeout: int | None = typer.Option(
        None,
        "--case-timeout",
        help=(
            "Batch mode: hard per-case watchdog in seconds. "
            "Defaults from run config when omitted."
        ),
    ),
    continue_on_error: bool | None = typer.Option(
        None,
        "--continue-on-error/--abort-on-error",
        help=(
            "Batch mode: keep running past failed cases and summarize them "
            "at the end (default from run config)."
        ),
    ),
    retry_passes: int | None = typer.Option(
        None,
        "--retry-passes",
        help=(
            "Batch mode: after the first pass, automatically re-scan and "
            "retry failed/incomplete cases up to this many extra passes."
        ),
    ),
) -> None:
    """Run a frozen release, an ad-hoc YAML batch, or a single case.

    With no explicit mode or configured release, batch mode runs the generated
    benchmark candidate catalog.
    """
    warn_legacy_operational_env()
    cfg = load_run_config(run_config)
    cfg = merge_cli(
        cfg,
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        access_role=access_role,
        result_dir=result_dir,
        batch_size=batch_size,
        case_timeout_sec=case_timeout,
        continue_on_error=continue_on_error,
        retry_passes=retry_passes,
        resume=resume,
        session_tag=session_tag,
        release=release,
    )
    set_run_config(cfg)
    apply_custom_provider_env(cfg)

    bench = cfg.benchmark
    resolved_batch_size = bench.batch_size
    resolved_resume = bench.resume
    resolved_continue = bench.continue_on_error
    resolved_retry = bench.retry_passes
    resolved_session_tag = session_tag if session_tag is not None else bench.session_tag
    resolved_result_dir = result_dir if result_dir is not None else cfg.nika.result_dir
    # Prefer CLI --release; else YAML benchmark.release
    resolved_release = release if release is not None else bench.release

    if scenario is not None and (config is not None or release is not None):
        raise typer.BadParameter(
            "Use either SCENARIO (single-case), --config (ad-hoc YAML), or "
            "--release (frozen suite), not a combination."
        )
    if config is not None and release is not None:
        raise typer.BadParameter("Use either --config or --release, not both.")

    single_mode = scenario is not None

    if single_mode:
        if batch_size is not None and batch_size != 1:
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
            result_dir=resolved_result_dir,
            session_tag=resolved_session_tag,
        )
        return

    if problem is not None:
        raise typer.BadParameter(
            "--problem without SCENARIO is invalid; pass SCENARIO or use "
            "--release / --config batch mode."
        )

    if config is None and resolved_release is None:
        config = Path(default_benchmark_yaml_path())

    if config is not None:
        run_benchmark_from_yaml(
            benchmark_file=str(config),
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            batch_size=resolved_batch_size,
            result_dir=resolved_result_dir,
            resume=resolved_resume,
            session_tag=resolved_session_tag,
            case_timeout=(
                case_timeout if case_timeout is not None else bench.case_timeout_sec
            ),
            continue_on_error=resolved_continue,
            retry_passes=resolved_retry,
        )
        return

    # Release path
    try:
        split_override = bench.split
        _, version = parse_release_ref(resolved_release)
        resolved_split = (
            normalize_split(split_override, default="test")
            if split_override
            else _default_split_for_version(version)
        )
        run_benchmark_from_release(
            release_ref=resolved_release,
            split=resolved_split,
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            batch_size=resolved_batch_size,
            result_dir=resolved_result_dir,
            resume=resolved_resume,
            session_tag=resolved_session_tag,
            case_timeout=(
                case_timeout if case_timeout is not None else bench.case_timeout_sec
            ),
            continue_on_error=resolved_continue,
            retry_passes=resolved_retry,
        )
    except (ReleaseError, ValueError) as exc:
        _exit_release_error(exc)


@benchmark_app.command("generate")
def benchmark_generate(
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Pool directory (default: benchmark/working/pool).",
    ),
) -> None:
    """Generate the executable candidate pool under ``benchmark/working/pool``."""
    from nika.workflows.benchmark.generate import generate_candidate_catalog

    catalog_dir, report = generate_candidate_catalog(out_path=output)
    typer.echo(
        f"Generated {report['summary']['candidate_files']} candidate files "
        f"({report['summary']['concrete_inject_options']} inject options) → {catalog_dir}"
    )


@benchmark_app.command("select")
def benchmark_select(
    pool: Path | None = typer.Option(
        None,
        "--pool",
        help="Candidate pool directory (default: benchmark/working/pool).",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Selected cases path (default: benchmark/working/cases.yaml).",
    ),
    seed: int = typer.Option(42, "--seed", help="Selection random seed."),
    skip_audit: bool = typer.Option(
        False,
        "--skip-audit",
        help="Skip pool audit gate (not recommended).",
    ),
) -> None:
    """Select a compact subset from the audited candidate pool."""
    from nika.workflows.benchmark.select_catalog import write_selected_catalog

    try:
        coverage = write_selected_catalog(
            pool=pool,
            output=output,
            seed=seed,
            skip_audit=skip_audit,
        )
    except ValueError as exc:
        _exit_release_error(exc)
    summary = coverage["summary"]
    typer.echo("selected benchmark")
    for key, value in summary.items():
        typer.echo(f"  {key}: {value}")
    typer.echo(f"Wrote {coverage['output']}")


@benchmark_app.command("freeze")
def benchmark_freeze(
    version: str = typer.Option(..., "--version", help="Immutable release version."),
    source: Path = typer.Option(
        Path("benchmark/working/release-candidate"),
        "--source",
        help="Validated directory containing dev.yaml and test.yaml.",
    ),
) -> None:
    """Freeze a validated Dev/Test candidate as a benchmark release."""
    try:
        release = freeze_split_release(version=version, source_dir=source)
        preflight_release(release, check_images=False)
    except (ReleaseError, ValueError) as exc:
        _exit_release_error(exc)
    typer.echo(f"Frozen {release.ref} under {release.root}")


@benchmark_app.command("migrate")
def benchmark_migrate(
    input_path: Path = typer.Option(
        ..., "--input", help="Legacy or working benchmark YAML."
    ),
    output_path: Path = typer.Option(
        ..., "--output", help="Destination YAML with materialized root_causes."
    ),
    report_path: Path = typer.Option(
        ..., "--report", help="YAML report of unresolved cases."
    ),
    allow_unresolved: bool = typer.Option(
        False,
        "--allow-unresolved",
        help="Write output even when some cases cannot be mapped.",
    ),
) -> None:
    """Materialize structured root-cause ground truth for every case."""
    from nika.problems.rca import UnresolvedRootCauseError
    from nika.workflows.benchmark.migrate import migrate_benchmark_yaml

    try:
        report = migrate_benchmark_yaml(
            input_path=input_path,
            output_path=output_path,
            report_path=report_path,
            allow_unresolved=allow_unresolved,
        )
    except UnresolvedRootCauseError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Migrated {report['resolved']}/{report['case_count']} cases to {output_path}. "
        f"Unresolved: {report['unresolved_count']} (see {report_path})."
    )
