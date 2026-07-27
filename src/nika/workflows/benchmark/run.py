"""Batch or single-case benchmark runs (env → inject → agent → eval)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import ValidationError

from nika.config import BENCHMARK_DIR, resolve_results_root
from nika.evaluator.result_log import MESSAGES_FILENAME
from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.problems.prob_pool import get_problem_instance
from nika.utils.session import Session
from nika.utils.session_artifacts import RUN_FILENAME
from nika.utils.session_store import SessionStore
from nika.workflows.agent.run import start_agent
from nika.workflows.benchmark.trials import (
    Trial,
    count_completed_trials,
    expand_trials,
    merge_run_config,
    scan_trials,
    trial_dir,
)
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.benchmark.release import (
    BenchmarkRelease,
    DEFAULT_RELEASE_VERSION,
    SplitName,
    build_job_metadata,
    load_release,
    load_run_config,
    normalize_split,
    preflight_release,
    release_fields_for_session,
    write_job_metadata,
)
from nika.workflows.benchmark.run_progress import (
    update_progress,
    update_progress_from_scan,
    write_progress,
)
from nika.workflows.benchmark.resume import (
    benchmark_row_fingerprint,
    benchmark_row_from_case,
)
from nika.workflows.env.start import start_net_env
from nika.workflows.eval.session import eval_results, run_eval_metrics
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session

_BENCHMARK_DONE_PREFIX = "benchmark_done "


def default_benchmark_yaml_path() -> str:
    return str(BENCHMARK_DIR / "benchmark_selected.yaml")


def default_release_ref() -> str:
    return DEFAULT_RELEASE_VERSION


def _stamp_release_meta(session_id: str, release_meta: dict | None) -> None:
    if not release_meta:
        return
    session = Session().load_running_session(session_id=session_id)
    for key, value in release_fields_for_session(release_meta).items():
        session.update_session(key, value)


def _stamp_trial_meta(
    session_id: str,
    *,
    trial_id: str | None,
    trial_index: int | None,
    case_key: str | None,
) -> None:
    if not trial_id:
        return
    session = Session().load_running_session(session_id=session_id)
    session.update_session("trial_id", trial_id)
    if trial_index is not None:
        session.update_session("trial_index", trial_index)
    if case_key is not None:
        session.update_session("case_key", case_key)


def validate_inject_params(
    problem: str,
    scenario: str,
    topo_size: str,
    params: dict[str, str],
) -> None:
    """Raise ValueError if inject params do not satisfy the problem schema."""
    if not params:
        raise ValueError(
            f"Missing inject parameters for {problem!r}. "
            f"Use --config with a YAML case or pass complete --set key=value flags. "
            f"Run `nika failure describe {problem}` for required fields."
        )

    kwargs: dict = {}
    if topo_size:
        kwargs["topo_size"] = topo_size
    problem_inst = get_problem_instance(
        problem_names=[problem],
        scenario_name=scenario,
        **kwargs,
    )
    params_class = getattr(type(problem_inst), "Params", None)
    if params_class is None:
        if params:
            raise ValueError(f"Problem {problem!r} does not accept inject parameters.")
        return
    try:
        params_class(**params)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid or incomplete inject parameters for {problem!r}: {exc}. "
            f"Run `nika failure describe {problem}` for required fields."
        ) from exc


def _ensure_messages_file(session_dir: Path) -> None:
    path = session_dir / MESSAGES_FILENAME
    if not path.exists():
        path.write_text("", encoding="utf-8")


def _set_trial_outcome(session_dir: Path, *, outcome: str) -> None:
    run_path = session_dir / RUN_FILENAME
    run_meta = json.loads(run_path.read_text(encoding="utf-8"))
    run_meta["outcome"] = outcome
    run_meta["status"] = "finished"
    run_path.write_text(json.dumps(run_meta, indent=2, default=str), encoding="utf-8")


def _finalize_agent_failed_trial(
    *,
    session_id: str,
    session_dir: Path,
    result_dir: str | None,
    error: BaseException,
) -> None:
    """Mark a post-inject failure as a counted ``agent_failed`` trial."""
    try:
        close_session(session_id=session_id, undeploy=True)
    except Exception as cleanup_error:  # noqa: BLE001 - best effort
        print(f"WARNING: could not clean up session {session_id}: {cleanup_error}")

    _ensure_messages_file(session_dir)
    try:
        run_eval_metrics(session_id=session_id, result_dir=result_dir)
    except Exception as eval_error:  # noqa: BLE001 - still record failure
        print(
            f"WARNING: could not write eval metrics for agent_failed "
            f"trial {session_id}: {eval_error}"
        )
        metrics_path = session_dir / "eval_metrics.json"
        if not metrics_path.exists():
            metrics_path.write_text(
                json.dumps(
                    {
                        "detection_score": -1.0,
                        "localization_accuracy": -1.0,
                        "localization_precision": -1.0,
                        "localization_recall": -1.0,
                        "localization_f1": -1.0,
                        "rca_accuracy": -1.0,
                        "rca_precision": -1.0,
                        "rca_recall": -1.0,
                        "rca_f1": -1.0,
                        "in_tokens": None,
                        "out_tokens": None,
                        "steps": None,
                        "tool_calls": None,
                        "tool_errors": None,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    try:
        session = Session().load_closed_session(
            session_id=session_id, result_dir=result_dir
        )
        session.update_run_meta("outcome", "agent_failed")
        session.update_run_meta("agent_error", str(error))
        session.update_run_meta("status", "finished")
    except Exception:  # noqa: BLE001 - fall back to direct file write
        _set_trial_outcome(session_dir, outcome="agent_failed")
        run_path = session_dir / RUN_FILENAME
        run_meta = json.loads(run_path.read_text(encoding="utf-8"))
        run_meta["agent_error"] = str(error)
        run_path.write_text(
            json.dumps(run_meta, indent=2, default=str), encoding="utf-8"
        )


def run_single_case(
    problem: str,
    scenario: str,
    topo_size: str,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    *,
    inject_params: dict[str, str],
    result_dir: str | None = None,
    session_tag: str | None = None,
    release_meta: dict | None = None,
    trial_id: str | None = None,
    trial_index: int | None = None,
    case_key: str | None = None,
) -> tuple[str, Path]:
    """Run one benchmark case (env → inject → agent → close + metrics).

    LLM judge and CSV summary are offline via ``nika eval judge`` /
    ``nika eval summary``.

    Returns:
        The session id and session directory for the completed run.
    """
    print(
        f"Running benchmark for Problem: {problem}, Scenario: {scenario}, Topo Size: {topo_size}"
        + (f", Trial: {trial_id}" if trial_id else "")
    )

    size = topo_size if topo_size else None
    if scenario_requires_topo_size(scenario) and not size:
        raise ValueError(
            f"Scenario '{scenario}' requires a non-empty topology size (-s s|m|l)."
        )
    if not scenario_requires_topo_size(scenario):
        size = None

    validate_inject_params(problem, scenario, topo_size or "", inject_params)
    params = dict(inject_params)

    predetermined_dir: str | None = None
    if trial_id:
        results_root = resolve_results_root(result_dir)
        resolved_case_key = case_key
        resolved_trial_index = trial_index
        if resolved_case_key is None or resolved_trial_index is None:
            if "__t" not in trial_id:
                raise ValueError(
                    f"Invalid trial_id {trial_id!r}; expected '{{case_key}}__tNN'."
                )
            key_part, index_part = trial_id.rsplit("__t", 1)
            resolved_case_key = resolved_case_key or key_part
            resolved_trial_index = resolved_trial_index or int(index_part)
        predetermined_dir = str(
            trial_dir(results_root, resolved_case_key, int(resolved_trial_index))
        )
        case_key = resolved_case_key
        trial_index = int(resolved_trial_index)

    session_id = start_net_env(
        scenario,
        size,
        redeploy=True,
        result_dir=result_dir,
        session_tag=session_tag,
        session_id=trial_id,
        session_dir=predetermined_dir,
    )
    session_dir = Path(SessionStore().get_session(session_id)["session_dir"])
    gt_written = False

    try:
        inject_failure(
            problem_names=[problem], session_id=session_id, param_overrides=params
        )
        gt_written = (session_dir / "ground_truth.json").is_file()

        row = benchmark_row_from_case(
            scenario=scenario,
            problem=problem,
            topo_size=topo_size,
            inject_params=params,
        )
        session = Session().load_running_session(session_id=session_id)
        session.update_session(
            "benchmark_fingerprint",
            benchmark_row_fingerprint(row),
        )
        _stamp_release_meta(session_id, release_meta)
        _stamp_trial_meta(
            session_id,
            trial_id=trial_id,
            trial_index=trial_index,
            case_key=case_key,
        )

        start_agent(
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            session_id=session_id,
            stream_output=False,
        )

        eval_results(session_id=session_id)
        _ensure_messages_file(session_dir)
        try:
            closed = Session().load_closed_session(
                session_id=session_id, result_dir=result_dir
            )
            closed.update_run_meta("outcome", "success")
        except Exception:  # noqa: BLE001 - still mark outcome on disk
            _set_trial_outcome(session_dir, outcome="success")
    except BaseException as exc:
        # Batch runs ( --config / --release): post-inject failures become
        # counted agent_failed outcomes. Bare single-case CLI (no trial_id)
        # still raises so abort-on-error behavior is preserved.
        if trial_id and (gt_written or (session_dir / "ground_truth.json").is_file()):
            _finalize_agent_failed_trial(
                session_id=session_id,
                session_dir=session_dir,
                result_dir=result_dir,
                error=exc,
            )
            print(
                f"{_BENCHMARK_DONE_PREFIX}session_id={session_id} scenario={scenario} "
                f"problem={problem} session_dir={session_dir} outcome=agent_failed"
            )
            return session_id, session_dir

        try:
            close_session(session_id=session_id, undeploy=True)
            print(f"cleaned up failed session {session_id} (lab undeployed)")
        except Exception as cleanup_error:  # noqa: BLE001 - best effort
            print(f"WARNING: could not clean up session {session_id}: {cleanup_error}")
        try:
            run_path = session_dir / RUN_FILENAME
            run_meta = json.loads(run_path.read_text(encoding="utf-8"))
            if run_meta.get("status") == "finished":
                run_meta["status"] = "error"
                run_path.write_text(json.dumps(run_meta, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 - best effort
            pass
        raise

    print(
        f"{_BENCHMARK_DONE_PREFIX}session_id={session_id} scenario={scenario} "
        f"problem={problem} session_dir={session_dir}"
    )
    return session_id, session_dir


def run_benchmark_from_yaml(
    benchmark_file: str,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    *,
    batch_size: int = 1,
    result_dir: str | None = None,
    resume: bool = True,
    session_tag: str | None = None,
    case_timeout: int = 0,
    continue_on_error: bool = False,
    retry_passes: int = 0,
    release_meta: dict | None = None,
) -> None:
    """Run ad-hoc YAML cases via the shared trial runner (``n_trials=1``).

    Results land under ``{result_dir}/trials/{case_key}__t01/``, matching release
    trials/ layout. Resume / batch / timeout / retry use the same orchestrator.
    """
    run_benchmark_trials(
        benchmark_file=benchmark_file,
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        n_trials=1,
        batch_size=batch_size,
        result_dir=result_dir,
        resume=resume,
        session_tag=session_tag,
        case_timeout=case_timeout,
        continue_on_error=continue_on_error,
        retry_passes=retry_passes,
        release_meta=release_meta,
    )


def _run_trial(
    trial: Trial,
    *,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    result_dir: str | None,
    session_tag: str | None,
    release_meta: dict | None,
) -> None:
    row = trial.row
    run_single_case(
        problem=row["problem"],
        scenario=row["scenario"],
        topo_size=row.get("topo_size") or "",
        inject_params=row["inject"],
        release_meta=release_meta,
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        result_dir=result_dir,
        session_tag=session_tag,
        trial_id=trial.trial_id,
        trial_index=trial.trial_index,
        case_key=trial.case_key,
    )


def _run_trial_with_timeout(
    trial: Trial,
    *,
    case_timeout: int,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    result_dir: str | None,
    session_tag: str | None,
    release_meta: dict | None,
    isolate: bool = False,
) -> None:
    """Run one trial; spawn a process when ``case_timeout`` > 0 or ``isolate``."""
    kwargs = dict(
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        result_dir=result_dir,
        session_tag=session_tag,
        release_meta=release_meta,
    )
    if case_timeout <= 0 and not isolate:
        _run_trial(trial, **kwargs)
        return

    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_run_trial, kwargs={"trial": trial, **kwargs})
    proc.start()
    join_timeout = case_timeout if case_timeout > 0 else None
    proc.join(join_timeout)
    if case_timeout > 0 and proc.is_alive():
        proc.terminate()
        proc.join(15)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        raise RuntimeError(
            f"[{trial.trial_id}] case exceeded --case-timeout ({case_timeout}s) "
            "and was killed. Its lab may be leaked — check `nika session ps`."
        )
    if proc.is_alive():
        proc.terminate()
        proc.join(15)
        raise RuntimeError(f"[{trial.trial_id}] trial worker did not exit")
    if proc.exitcode not in (0, None):
        raise RuntimeError(
            f"[{trial.trial_id}] trial worker exited with code {proc.exitcode}"
        )


def _run_trials_batch(
    trials_batch: list[Trial],
    *,
    continue_on_error: bool,
    case_timeout: int,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    result_dir: str | None,
    session_tag: str | None,
    release_meta: dict | None,
) -> list[str]:
    """Run a batch of trials; parallel batches use spawn processes for isolation."""
    failures: list[str] = []
    # Parallel Kathara/MCP work is not safe on shared in-process clients.
    isolate = len(trials_batch) > 1
    shared = dict(
        case_timeout=case_timeout,
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        result_dir=result_dir,
        session_tag=session_tag,
        release_meta=release_meta,
        isolate=isolate,
    )
    if len(trials_batch) == 1:
        trial = trials_batch[0]
        try:
            _run_trial_with_timeout(trial, **shared)
        except Exception as e:  # noqa: BLE001
            if not continue_on_error:
                raise
            print(f"TRIAL FAILED (continuing): [{trial.trial_id}] {e}")
            failures.append(f"[{trial.trial_id}] {e}")
        return failures

    with ThreadPoolExecutor(max_workers=len(trials_batch)) as pool:
        futures = {
            pool.submit(_run_trial_with_timeout, trial, **shared): trial
            for trial in trials_batch
        }
        for future in as_completed(futures):
            trial = futures[future]
            try:
                future.result()
            except Exception as e:  # noqa: BLE001
                if not continue_on_error:
                    raise
                print(f"TRIAL FAILED (continuing): [{trial.trial_id}] {e}")
                failures.append(f"[{trial.trial_id}] {e}")
    return failures


def run_benchmark_trials(
    benchmark_file: str,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    *,
    n_trials: int = 1,
    batch_size: int = 1,
    result_dir: str | None = None,
    resume: bool = True,
    session_tag: str | None = None,
    case_timeout: int = 0,
    continue_on_error: bool = False,
    retry_passes: int = 0,
    release_meta: dict | None = None,
) -> None:
    """Run cases × ``n_trials`` under ``{result_dir}/trials/`` (shared batch kernel)."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if retry_passes < 0:
        raise ValueError("retry_passes must be >= 0")
    if retry_passes and not continue_on_error:
        continue_on_error = True

    rows = load_benchmark_yaml(benchmark_file)
    if not rows:
        print(f"No benchmark rows found in {benchmark_file}")
        return

    trials = expand_trials(rows, n_trials)
    results_root = resolve_results_root(result_dir)
    run_id = None
    if release_meta:
        run_id = release_meta.get("run_id") or release_meta.get("job_id")

    def _refresh_progress(pending: list[int], *, status: str = "running") -> None:
        if not run_id:
            return
        update_progress_from_scan(
            str(run_id),
            result_dir=results_root,
            total_trials=len(trials),
            pending=pending,
            status=status,
            release_meta=release_meta,
        )

    def _finish_progress(pending: list[int]) -> None:
        if not run_id:
            return
        _refresh_progress(pending, status="finished")

    def _run_pending(pending: list[int]) -> list[str]:
        failures: list[str] = []
        for chunk_start in range(0, len(pending), batch_size):
            chunk_indices = pending[chunk_start : chunk_start + batch_size]
            batch = [trials[index] for index in chunk_indices]
            if len(batch) == 1 and case_timeout <= 0:
                trial = batch[0]
                print(f"{trial.label} {trial.trial_id} running")
            else:
                print(
                    f"[batch {chunk_start // batch_size + 1}] running "
                    f"{len(batch)} trial(s)"
                    + (" in parallel" if len(batch) > 1 else "")
                )
            failures += _run_trials_batch(
                batch,
                continue_on_error=continue_on_error,
                case_timeout=case_timeout,
                agent_type=agent_type,
                llm_provider=llm_provider,
                model=model,
                max_steps=max_steps,
                result_dir=str(results_root),
                session_tag=session_tag,
                release_meta=release_meta,
            )
            if run_id:
                completed = count_completed_trials(
                    trials=trials, result_dir=results_root
                )
                total = len(trials)
                update_progress(
                    str(run_id),
                    result_dir=results_root,
                    total_trials=total,
                    completed_trials=completed,
                    pending_trials=max(0, total - completed),
                    status="running",
                    release_meta=release_meta,
                )
        return failures

    failures: list[str] = []
    previous_pending: int | None = None
    for attempt in range(retry_passes + 1):
        _root, pending = scan_trials(
            trials=trials,
            result_dir=results_root,
            resume=resume or attempt > 0,
        )
        _refresh_progress(pending)
        if not pending:
            if attempt > 0:
                print("\nAll trials completed after retries.")
            _finish_progress([])
            return
        if attempt > 0:
            if previous_pending is not None and len(pending) >= previous_pending:
                print(
                    f"\nRetry made no progress ({len(pending)} trial(s) still "
                    "incomplete); stopping retries."
                )
                break
            print(
                f"\n[retry {attempt}/{retry_passes}] retrying {len(pending)} incomplete trial(s)"
            )
        previous_pending = len(pending)
        failures = _run_pending(pending)
        # agent_failed trials count as complete; only incomplete trials remain.
        _root, still_pending = scan_trials(
            trials=trials,
            result_dir=results_root,
            resume=True,
        )
        _refresh_progress(still_pending)
        if not still_pending:
            if attempt > 0:
                print("\nAll trials completed after retries.")
            _finish_progress([])
            return
        if not failures and still_pending:
            # agent_failed trials count as complete; remaining pending means
            # incomplete artifacts after the pass.
            failures = [
                f"incomplete trial {trials[i].trial_id}" for i in still_pending
            ]

    _, final_pending = scan_trials(
        trials=trials,
        result_dir=results_root,
        resume=True,
    )
    _finish_progress(final_pending)

    if failures:
        print(
            f"\n{len(failures)} trial(s) still FAILED "
            "(re-run the same command with --resume to retry only incomplete trials):"
        )
        for message in failures:
            print(f"  - {message.splitlines()[0]}")



def run_benchmark_from_release(
    release_ref: str,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    *,
    split: SplitName | str = "test",
    batch_size: int = 1,
    result_dir: str | None = None,
    resume: bool = True,
    session_tag: str | None = None,
    case_timeout: int | None = None,
    continue_on_error: bool = False,
    retry_passes: int = 0,
    check_images: bool = True,
    release: BenchmarkRelease | None = None,
) -> None:
    """Run a frozen ``nika-bench`` release split after preflight validation."""
    resolved_split = normalize_split(split, default="test")
    resolved = release or load_release(release_ref, split=resolved_split)
    if resolved.split != resolved_split:
        resolved = load_release(release_ref, split=resolved_split)
    preflight_release(resolved, check_images=check_images)

    # Release defaults supply a recommended watchdog; CLI may override freely.
    effective_timeout = (
        resolved.case_timeout_sec if case_timeout is None else case_timeout
    )
    n_trials = resolved.n_trials
    official = True

    results_root = resolve_results_root(result_dir)
    proposed = build_job_metadata(
        resolved,
        agent_type=agent_type,
        model=model,
        llm_provider=llm_provider,
        max_steps=max_steps,
        n_trials=n_trials,
        case_timeout_sec=effective_timeout,
        official=official,
    )
    existing = load_run_config(results_root)
    job = merge_run_config(existing=existing, proposed=proposed)
    job_path = write_job_metadata(results_root, job)
    run_id = str(job.get("run_id") or job.get("job_id"))
    total_trials = int(resolved.case_count) * int(n_trials)
    write_progress(
        run_id,
        result_dir=results_root,
        status="running",
        total_trials=total_trials,
        completed_trials=0,
        pending_trials=total_trials,
        benchmark_id=job.get("benchmark_id"),
        version=job.get("version"),
        agent_type=job.get("agent_type"),
        model=job.get("model"),
    )
    print(
        f"Running {resolved.ref} split={resolved.split} "
        f"({resolved.case_count} cases × {n_trials} trials, "
        f"digest={resolved.benchmark_digest[:12]}…, official={official}) "
        f"→ {job_path}"
    )

    run_benchmark_trials(
        benchmark_file=str(resolved.cases_path),
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        n_trials=n_trials,
        batch_size=batch_size,
        result_dir=str(results_root),
        resume=resume,
        session_tag=session_tag,
        case_timeout=effective_timeout,
        continue_on_error=continue_on_error,
        retry_passes=retry_passes,
        release_meta=job,
    )
