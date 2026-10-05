"""Batch or single-case benchmark runs (env → inject → agent → eval)."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nika.config import BENCHMARK_DIR, resolve_results_root
from nika.evaluator.result_log import MESSAGES_FILENAME
from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.problems.registry import get_problem_class, get_problem_instance
from nika.utils.session import Session
from nika.utils.session_artifacts import (
    RUN_FILENAME,
    last_session_error,
    normalize_session_status,
    update_run_json,
    write_json_atomic,
)
from nika.utils.session_store import SessionStore
from nika.workflows.agent.run import start_agent
from nika.workflows.benchmark.admit import (
    pick_admissible,
    resource_class,
    tier_limits,
)
from nika.workflows.benchmark.display import (
    BenchmarkProgress,
    OutputMode,
    RunPlan,
    apply_worker_warning_env,
    confirm_run,
    print_deferred_warnings,
    print_inspect_hint,
    print_run_plan,
    quiet_third_party_logging,
    read_trial_metrics,
    vprint,
)
from nika.workflows.benchmark.healthy import (
    is_healthy_case,
    write_healthy_session_artifacts,
)
from nika.workflows.benchmark.isp_options import ISP_DEPLOY_KEYS
from nika.workflows.benchmark.load_config import load_benchmark_input
from nika.workflows.benchmark.multi_fault import flatten_inject_overrides, row_problems
from nika.workflows.benchmark.outcomes import (
    COUNTED_OUTCOMES,
    RETRYABLE_OUTCOMES,
    classify_trial_failure,
    is_signal_exit_code,
    trace_ends_in_llm_call,
)
from nika.evaluator.score_status import (
    is_infra_error_evidence,
    resolve_score_status,
    scores_for_status,
)
from nika.workflows.benchmark.release import (
    DEFAULT_RELEASE_VERSION,
    RUN_CONFIG_FILENAME,
    BenchmarkRelease,
    SplitName,
    build_job_metadata,
    build_run_identity,
    load_release,
    load_run_config,
    normalize_split,
    preflight_release,
    release_fields_for_session,
    write_job_metadata,
)
from nika.workflows.benchmark.resume import (
    benchmark_row_fingerprint,
    benchmark_row_from_case,
)
from nika.workflows.benchmark.run_progress import (
    update_progress,
    update_progress_from_scan,
    write_progress,
)
from nika.workflows.benchmark.trials import (
    Trial,
    _restore_success_eval_metrics,
    expand_trials,
    has_valid_submission,
    heal_trial_outcome,
    is_finalized_failure,
    is_valid_trial,
    merge_run_config,
    scan_trials,
    select_trials,
    store_session_id_for_trial,
    trial_dir,
    trial_outcome,
)
from nika.workflows.env.start import start_net_env
from nika.workflows.eval.session import (
    eval_results,
    run_eval_metrics,
)
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session, load_session_meta_for_close


def _maybe_start_inspect(
    results_root: Path, *, output_mode: OutputMode = "human"
) -> str | None:
    """Best-effort background inspect for interactive human (TTY) runs."""
    import sys

    if output_mode != "human" or not sys.stdout.isatty():
        return None
    try:
        from nika.inspect.serve import start_inspect_background

        return start_inspect_background(result_dir=results_root)
    except Exception as exc:  # noqa: BLE001 - advisory; never fail the run
        print(f"WARNING: could not start nika inspect: {exc}")
        return None


_BENCHMARK_DONE_PREFIX = "benchmark_done "

_ISP_BANNER_LABELS = {
    "topo": "Topo",
    "igp": "IGP",
    "bgp_mode": "BGP",
    "rpki": "RPKI",
    "backend": "Backend",
    "device_profile": "Device",
}

# Worker processes for timed/isolated trials. Ctrl+C reaches only the main
# thread, so ThreadPoolExecutor workers blocked in ``Process.join`` never see
# KeyboardInterrupt — the parent must terminate these explicitly.
_active_trial_procs: set[Any] = set()
_active_trial_procs_lock = threading.Lock()
# Set while the parent is stopping workers (Ctrl+C or --abort-on-error) so pool
# threads do not stamp killed trials as counted outcomes (slot stays retryable).
_interrupt_cleanup_active = False
# How long the parent waits for workers to close their own labs.
_WORKER_INTERRUPT_GRACE_SEC = 60


def _is_user_interrupt(exc: BaseException) -> bool:
    return isinstance(exc, (KeyboardInterrupt, SystemExit))


def _register_trial_proc(proc: Any) -> None:
    with _active_trial_procs_lock:
        _active_trial_procs.add(proc)


def _unregister_trial_proc(proc: Any) -> None:
    with _active_trial_procs_lock:
        _active_trial_procs.discard(proc)


def _is_alive(proc: Any) -> bool:
    return bool(getattr(proc, "is_alive", lambda: False)())


def _stop_worker(proc: Any, *, grace_sec: float = _WORKER_INTERRUPT_GRACE_SEC) -> None:
    """SIGTERM one worker, let its cleanup (lab undeploy) run, then SIGKILL."""
    if not _is_alive(proc):
        return
    proc.terminate()
    proc.join(grace_sec)
    if _is_alive(proc):
        proc.kill()
        proc.join(5)


def _terminate_active_trial_workers(*, signal_first: bool = False) -> None:
    """Let in-flight trial workers undeploy, then SIGTERM/SIGKILL stragglers.

    After Ctrl+C the terminal already delivered SIGINT to every worker, so the
    parent only waits. ``signal_first`` (``--abort-on-error``) sends SIGTERM
    first; the worker's SIGTERM handler runs the same cleanup.
    """
    global _interrupt_cleanup_active
    _interrupt_cleanup_active = True
    with _active_trial_procs_lock:
        procs = list(_active_trial_procs)
    alive = [p for p in procs if _is_alive(p)]
    if alive:
        if signal_first:
            for proc in alive:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001 - best effort
                    pass
        # SIGKILL mid-undeploy leaks half-removed labs; give cleanup time.
        print(
            f"Waiting up to {_WORKER_INTERRUPT_GRACE_SEC}s for {len(alive)} trial "
            "worker(s) to undeploy their labs (Ctrl+C again to skip)…"
        )
        deadline = time.monotonic() + _WORKER_INTERRUPT_GRACE_SEC
        try:
            for proc in alive:
                proc.join(max(0.0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            pass
    for proc in procs:
        try:
            _stop_worker(proc, grace_sec=10)
        except Exception:  # noqa: BLE001 - best effort
            pass
        finally:
            _unregister_trial_proc(proc)


def _session_belongs_to_result_dir(
    *,
    session_id: str,
    session_dir: str | Path | None,
    result_root: Path,
) -> Path | None:
    """Return the session_dir path when this running session is part of the run.

    When the running record has an explicit ``session_dir``, only match if it
    lives under ``result_root``. Do not fall back to ``trials/{session_id}`` in
    that case — deterministic trial ids are shared across concurrent runs.
    """
    if session_dir:
        try:
            resolved = Path(session_dir).resolve()
            # Job result roots share run.json with trials/; equality is not a
            # trial session (Path.is_relative_to is true for the path itself).
            if resolved != result_root and resolved.is_relative_to(result_root):
                return resolved
        except (OSError, RuntimeError, ValueError):
            pass
        return None
    trial_path = result_root / "trials" / session_id
    if trial_path.is_dir():
        return trial_path.resolve()
    legacy = result_root / session_id
    if legacy.is_dir() and legacy.resolve() != result_root:
        return legacy.resolve()
    return None


def cleanup_benchmark_interrupt(
    result_dir: str | Path | None, *, signal_workers: bool = False
) -> int:
    """Undeploy labs for running sessions under ``result_dir`` after Ctrl+C.

    Stops isolated trial workers first so they cannot race with undeploy
    (``signal_workers`` sends them SIGTERM, for ``--abort-on-error``), then
    closes every still-running session whose artifacts live under this run.
    Marks each closed trial ``run.json`` as ``status=aborted`` so inspect can
    distinguish Ctrl+C from a normal finish.

    Also rewrites incomplete trial dirs under ``result_dir/trials`` that a
    worker already cleared as ``finished`` (race with ``clear_session``) so
    inspect does not show a clean finish for a mid-run stop.

    Returns how many sessions close was attempted for.
    """
    _terminate_active_trial_workers(signal_first=signal_workers)
    if result_dir is None:
        return 0
    result_root = Path(result_dir).resolve()
    closed = 0
    try:
        running = SessionStore().list_running_sessions()
    except Exception as list_error:  # noqa: BLE001 - still try nothing
        print(f"WARNING: could not list running sessions after interrupt: {list_error}")
        running = []

    for row in running:
        session_id = str(row.get("session_id") or "")
        if not session_id:
            continue
        matched = _session_belongs_to_result_dir(
            session_id=session_id,
            session_dir=row.get("session_dir"),
            result_root=result_root,
        )
        if matched is None:
            # Job result roots were historically indexed as sessions named after
            # the folder (e.g. results/claude → session_id=claude). Clear the
            # orphan index row; do not touch job run.json or undeploy.
            raw_dir = row.get("session_dir")
            if not raw_dir:
                continue
            try:
                if Path(raw_dir).resolve() != result_root:
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            try:
                close_session(
                    session_id=session_id,
                    undeploy=False,
                    session_dir=result_root,
                )
                closed += 1
            except Exception as cleanup_error:  # noqa: BLE001 - best effort
                print(
                    f"WARNING: could not clear orphan session index entry "
                    f"{session_id}: {cleanup_error}"
                )
            continue
        try:
            close_session(
                session_id=session_id,
                undeploy=True,
                session_dir=matched,
                status="aborted",
            )
            _mark_session_aborted(matched)
            closed += 1
            print(f"cleaned up interrupted session {session_id} (lab undeployed)")
        except Exception as cleanup_error:  # noqa: BLE001 - best effort
            print(
                f"WARNING: could not clean up interrupted session "
                f"{session_id}: {cleanup_error}"
            )
    closed += _abort_incomplete_trials(result_root)
    return closed


def _abort_incomplete_trials(result_root: Path) -> int:
    """Stamp aborted on incomplete trials left as finished by worker clear.

    Skips counted ``success`` / ``agent_failed`` slots. Returns how many
    trial dirs were rewritten.
    """
    trials_root = result_root / "trials"
    if not trials_root.is_dir():
        return 0
    rewritten = 0
    try:
        children = list(trials_root.iterdir())
    except OSError:
        return 0
    for trial_dir_path in children:
        if not trial_dir_path.is_dir():
            continue
        run_path = trial_dir_path / RUN_FILENAME
        if not run_path.is_file():
            continue
        try:
            run_meta = json.loads(run_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(run_meta, dict):
            continue
        if is_valid_trial(trial_dir_path):
            continue
        outcome = str(run_meta.get("outcome") or "")
        if outcome in COUNTED_OUTCOMES:
            continue
        if normalize_session_status(run_meta) == "aborted":
            continue
        _mark_session_aborted(trial_dir_path)
        rewritten += 1
    return rewritten


def _mark_session_aborted(session_dir: Path) -> None:
    """Stamp ``run.json`` as aborted after Ctrl+C cleanup (overrides finished)."""

    def _abort(run_meta: dict[str, Any]) -> None:
        run_meta["status"] = "aborted"
        if not run_meta.get("outcome"):
            run_meta["outcome"] = "aborted"

    update_run_json(session_dir, _abort)


def default_benchmark_yaml_path() -> str:
    return str(BENCHMARK_DIR / "working" / "pool")


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
    params: dict[str, Any],
    *,
    problems: list[str] | None = None,
) -> None:
    """Raise ValueError if inject params do not satisfy the problem schema."""
    resolved_problems = list(problems or row_problems({"problem": problem}))
    if is_healthy_case(problem):
        if params:
            raise ValueError(
                f"Healthy case {problem!r} does not accept inject parameters."
            )
        return
    if not params:
        raise ValueError(
            f"Missing inject parameters for {problem!r}. "
            f"Use --config with a YAML case or pass complete --set key=value flags. "
            f"Run `nika failure describe {problem}` for required fields."
        )

    kwargs: dict = {}
    if topo_size:
        kwargs["topo_size"] = topo_size
    if len(resolved_problems) > 1:
        nested = flatten_inject_overrides(
            {"problem": problem, "problems": resolved_problems, "inject": params}
        )
        problem_inst = get_problem_instance(
            problem_names=resolved_problems,
            scenario_name=scenario,
            **kwargs,
        )
        if hasattr(problem_inst, "resolve_params"):
            problem_inst.resolve_params(nested)
        return

    problem_cls = get_problem_class(resolved_problems[0], scenario)
    if problem_cls is None:
        raise ValueError(f"Unknown problem {resolved_problems[0]!r}")
    params_class = getattr(problem_cls, "Params", None)
    if params_class is None:
        if params:
            raise ValueError(
                f"Problem {resolved_problems[0]!r} does not accept inject parameters."
            )
        return
    try:
        params_class(**params)
    except ValidationError as exc:
        raise ValueError(
            f"Invalid or incomplete inject parameters for {resolved_problems[0]!r}: {exc}. "
            f"Run `nika failure describe {resolved_problems[0]}` for required fields."
        ) from exc


def _ensure_messages_file(session_dir: Path) -> None:
    path = session_dir / MESSAGES_FILENAME
    if not path.exists():
        path.write_text("", encoding="utf-8")


def _require_submission(session_dir: Path) -> None:
    """Treat an agent return without a submission as an agent failure."""
    if not has_valid_submission(session_dir):
        raise RuntimeError(
            "Agent completed without writing required submission: "
            f"{session_dir / 'submission.json'}"
        )


def _ensure_placeholder_eval_metrics(
    session_dir: Path, *, score_status: str = "no_submission"
) -> None:
    metrics_path = session_dir / "eval_metrics.json"
    if metrics_path.exists():
        return
    scores = scores_for_status(score_status)  # type: ignore[arg-type]
    write_json_atomic(
        metrics_path,
        {
            **scores,
            "in_tokens": None,
            "out_tokens": None,
            "steps": None,
            "tool_calls": None,
            "tool_errors": None,
        },
    )


def _set_trial_outcome(
    session_dir: Path,
    *,
    outcome: str,
    status: str = "finished",
    agent_error: str | None = None,
    score_status: str | None = None,
) -> None:
    def _stamp(run_meta: dict[str, Any]) -> None:
        run_meta["outcome"] = outcome
        run_meta["status"] = status
        if agent_error is not None:
            run_meta["agent_error"] = agent_error
        if score_status is not None:
            run_meta["score_status"] = score_status
        # Watchdog kills never reach session.end_session(); stamp end_time here
        # so inspect Duration is not "—" for counted agent_failed / error slots.
        if not run_meta.get("end_time"):
            run_meta["end_time"] = datetime.now().isoformat()

    update_run_json(session_dir, _stamp)


def _close_quietly(
    session_id: str, session_dir: Path, *, status: str = "finished"
) -> None:
    try:
        close_session(
            session_id=session_id,
            undeploy=True,
            session_dir=session_dir,
            status=status,  # type: ignore[arg-type]
        )
    except FileNotFoundError:
        pass  # Already closed (worker cleanup) or never registered.
    except Exception as cleanup_error:  # noqa: BLE001 - best effort
        print(f"WARNING: could not clean up session {session_id}: {cleanup_error}")


def _stamp_closed_outcome(
    *,
    session_id: str,
    session_dir: Path,
    result_dir: str | None,
    outcome: str,
    status: str,
    agent_error: str | None,
) -> None:
    """Record the outcome through the closed session (updates the index too)."""
    try:
        session = Session().load_closed_session(
            session_id=session_id, result_dir=result_dir, session_dir=session_dir
        )
        session.update_run_meta("outcome", outcome)
        if agent_error is not None:
            session.update_run_meta("agent_error", agent_error)
        session.update_run_meta("status", status)
    except Exception:  # noqa: BLE001 - fall back to direct file write
        _set_trial_outcome(
            session_dir, outcome=outcome, status=status, agent_error=agent_error
        )


def _finalize_failed_trial(
    *,
    session_id: str,
    session_dir: Path,
    result_dir: str | None,
    error: BaseException,
    outcome: str,
) -> None:
    """Close the lab and stamp a post-inject failure outcome with ``score_status``.

    ``agent_failed`` is a counted finished slot (kept by resume).
    ``endpoint_failed`` / ``infra_failed`` are retryable: ``status=error``, not
    counted, cleaned on resume — no eval_metrics placeholders for those.
    """
    status = "finished" if outcome == "agent_failed" else "error"
    _close_quietly(session_id, session_dir, status=status)
    _ensure_messages_file(session_dir)

    has_submission = has_valid_submission(session_dir)
    has_ground_truth = (session_dir / "ground_truth.json").is_file()
    if has_submission:
        # Submission-first: agent finished even if a later step failed.
        outcome = "success"
        status = "finished"
        score_status = "scored"
    elif outcome == "infra_failed":
        score_status = "infra_error"
    elif outcome == "endpoint_failed":
        score_status = resolve_score_status(
            has_submission=False,
            infra_evidence=False,
            has_ground_truth=has_ground_truth,
        )
    else:
        score_status = resolve_score_status(
            has_submission=False,
            infra_evidence=is_infra_error_evidence(error),
            has_ground_truth=has_ground_truth,
        )

    _set_trial_outcome(
        session_dir,
        outcome=outcome,
        status=status,
        agent_error=str(error),
        score_status=score_status,
    )

    # Counted finished slots get metrics; retryable error slots do not.
    if status == "finished":
        try:
            run_eval_metrics(
                session_id=session_id,
                result_dir=result_dir,
                session_dir=session_dir,
                infra_evidence=score_status == "infra_error",
            )
        except Exception as eval_error:  # noqa: BLE001 - still record failure
            print(
                f"WARNING: could not write eval metrics for "
                f"trial {session_id}: {eval_error}"
            )
            if has_valid_submission(session_dir):
                score_status = "grading_error"
                outcome = "success"
            _ensure_placeholder_eval_metrics(session_dir, score_status=score_status)
            _set_trial_outcome(
                session_dir,
                outcome=outcome,
                status=status,
                agent_error=str(error),
                score_status=score_status,
            )

    _stamp_closed_outcome(
        session_id=session_id,
        session_dir=session_dir,
        result_dir=result_dir,
        outcome=outcome,
        status=status,
        agent_error=str(error),
    )
    try:
        session = Session().load_closed_session(
            session_id=session_id, result_dir=result_dir, session_dir=session_dir
        )
        session.update_run_meta("score_status", score_status)
    except Exception:  # noqa: BLE001 - best effort
        pass


def _finalize_post_inject_failure(
    *,
    session_id: str,
    session_dir: Path,
    result_dir: str | None,
    error: BaseException,
) -> str:
    """Finalize a trial that failed after ground truth was written.

    A valid ``submission.json`` means the agent finished its task, so the trial
    is a ``success`` even when a later step (agent timeout, sandbox teardown,
    worker kill) failed. Otherwise classify the failure. Returns the outcome.
    """
    if has_valid_submission(session_dir):
        _close_and_eval_success(
            session_id=session_id,
            session_dir=session_dir,
            result_dir=result_dir,
            agent_error=str(error),
        )
        return "success"
    outcome = classify_trial_failure(error, session_dir=session_dir)
    _finalize_failed_trial(
        session_id=session_id,
        session_dir=session_dir,
        result_dir=result_dir,
        error=error,
        outcome=outcome,
    )
    return outcome


def _finalize_timed_out_trial(
    trial: Trial,
    *,
    result_dir: str | None,
    error: BaseException,
) -> None:
    """After a watchdog kill, stamp a failure when GT was already written."""
    results_root = resolve_results_root(result_dir)
    session_dir = trial_dir(results_root, trial.case_key, trial.trial_index)
    session_id = (
        store_session_id_for_trial(trial.trial_id, result_dir) or trial.trial_id
    )
    if not (session_dir / "ground_truth.json").is_file():
        # Killed during deploy/inject: nothing to score, but undeploy the lab
        # so it cannot overlap the next (possibly exclusive) trial.
        _close_quietly(session_id, session_dir, status="error")
        return
    outcome = _finalize_post_inject_failure(
        session_id=session_id,
        session_dir=session_dir,
        result_dir=result_dir,
        error=error,
    )
    print(f"[{trial.trial_id}] finalized killed trial as {outcome} under {session_dir}")


def _close_and_eval_success(
    *,
    session_id: str,
    session_dir: Path,
    result_dir: str | None,
    agent_error: str | None = None,
) -> None:
    """Close, stamp ``outcome=success`` ASAP, then write eval metrics.

    ``agent_error`` records a failure that happened after the submission.
    """
    # A teardown hiccup must not turn a valid submission into agent_failed.
    _close_quietly(session_id, session_dir)
    _ensure_messages_file(session_dir)
    _set_trial_outcome(
        session_dir, outcome="success", agent_error=agent_error, score_status="scored"
    )
    try:
        run_eval_metrics(
            session_id=session_id, result_dir=result_dir, session_dir=session_dir
        )
    except Exception as eval_error:  # noqa: BLE001 - outcome already stamped
        print(
            f"WARNING: could not write eval metrics for success "
            f"trial {session_id}: {eval_error}"
        )
        # Rebuild from on-disk artifacts; never write -1 placeholders for a
        # success, which would block --resume from repairing the metrics.
        _restore_success_eval_metrics(session_dir)
    _stamp_closed_outcome(
        session_id=session_id,
        session_dir=session_dir,
        result_dir=result_dir,
        outcome="success",
        status="finished",
        agent_error=agent_error,
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
    inject_params: dict[str, Any],
    problems: list[str] | None = None,
    result_dir: str | None = None,
    session_tag: str | None = None,
    release_meta: dict | None = None,
    trial_id: str | None = None,
    trial_index: int | None = None,
    case_key: str | None = None,
    expected_root_causes: list | None = None,
    candidate_option_id: str | None = None,
    topo: str | None = None,
    igp: str | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
    backend: str | None = None,
    device_profile: str | None = None,
    verbose: bool = False,
    progress: BenchmarkProgress | None = None,
    progress_label: str | None = None,
) -> tuple[str, Path]:
    """Run one benchmark case (env → inject → agent → close + metrics).

    LLM judge and CSV summary are offline via ``nika eval judge`` /
    ``nika eval summary``.

    Returns:
        The session id and session directory for the completed run.
    """

    def _phase(name: str) -> None:
        if progress is not None and progress_label:
            progress.set_phase(progress_label, name)

    row = benchmark_row_from_case(
        scenario=scenario,
        problem=problem,
        topo_size=topo_size,
        inject_params=inject_params,
        topo=topo,
        igp=igp,
        bgp_mode=bgp_mode,
        rpki=rpki,
        backend=backend,
        device_profile=device_profile,
    )
    isp_bits = [
        f"{_ISP_BANNER_LABELS[key]}: {'on' if key == 'rpki' else row[key]}"
        for key in ISP_DEPLOY_KEYS
        if row.get(key)
    ]
    # Batch trials stay quiet unless -v; bare single-case CLI keeps the banner.
    if verbose or not trial_id:
        print(
            f"Running benchmark for Problem: {problem}, Scenario: {scenario}, "
            f"Topo Size: {topo_size}"
            + (f", {', '.join(isp_bits)}" if isp_bits else "")
            + (f", Trial: {trial_id}" if trial_id else "")
        )

    if not verbose:
        quiet_third_party_logging()

    size = topo_size if topo_size else None
    if scenario_requires_topo_size(scenario) and not size:
        raise ValueError(
            f"Scenario '{scenario}' requires a non-empty topology size (-s s|m|l)."
        )
    if not scenario_requires_topo_size(scenario):
        size = None

    resolved_problems = list(
        problems or row_problems({"problem": problem, "inject": inject_params})
    )
    inject_overrides = (
        flatten_inject_overrides(
            {
                "problem": problem,
                "problems": resolved_problems,
                "inject": inject_params,
            }
        )
        if len(resolved_problems) > 1
        else dict(inject_params)
    )

    validate_inject_params(
        problem,
        scenario,
        topo_size or "",
        inject_params,
        problems=resolved_problems,
    )

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

    if progress is not None and progress_label and predetermined_dir:
        progress.attach_session(progress_label, predetermined_dir)

    _phase("deploy")
    # Trial directory stays ``trials/{trial_id}``; SessionStore key must also
    # incorporate result_dir so parallel same-task runs do not share a lab.
    store_session_id = store_session_id_for_trial(trial_id, result_dir) or trial_id
    session_id = start_net_env(
        scenario,
        size,
        redeploy=True,
        result_dir=result_dir,
        session_tag=session_tag,
        session_id=store_session_id,
        session_dir=predetermined_dir,
        topo=topo,
        igp=igp,
        bgp_mode=bgp_mode,
        rpki=rpki,
        backend=backend,
        device_profile=device_profile,
    )
    session_dir = Path(predetermined_dir) if predetermined_dir else None
    gt_written = False

    try:
        if session_dir is None:
            session_dir = Path(load_session_meta_for_close(session_id)["session_dir"])
        if is_healthy_case(problem):
            write_healthy_session_artifacts(session_id)
        else:
            _phase("inject")
            inject_failure(
                problem_names=resolved_problems,
                session_id=session_id,
                param_overrides=inject_overrides,
                expected_root_causes=expected_root_causes,
            )
        gt_written = (session_dir / "ground_truth.json").is_file()

        session = Session().load_running_session(session_id=session_id)
        session.update_session(
            "benchmark_fingerprint",
            benchmark_row_fingerprint(row),
        )
        if candidate_option_id:
            session.update_session("candidate_option_id", candidate_option_id)
        _stamp_release_meta(session_id, release_meta)
        _stamp_trial_meta(
            session_id,
            trial_id=trial_id,
            trial_index=trial_index,
            case_key=case_key,
        )

        _phase("agent")
        start_agent(
            agent_type=agent_type,
            llm_provider=llm_provider,
            model=model,
            max_steps=max_steps,
            session_id=session_id,
            stream_output=False,
            check_fault_presence=True,
        )
        _require_submission(session_dir)

        _phase("eval")
        if trial_id:
            # Batch trials: close then stamp outcome before metrics so a kill
            # mid-eval still leaves a counted success for --resume.
            _close_and_eval_success(
                session_id=session_id,
                session_dir=session_dir,
                result_dir=result_dir,
            )
        else:
            eval_results(session_id=session_id)
            _ensure_messages_file(session_dir)
            try:
                closed = Session().load_closed_session(
                    session_id=session_id,
                    result_dir=result_dir,
                    session_dir=session_dir,
                )
                closed.update_run_meta("outcome", "success")
            except Exception:  # noqa: BLE001 - still mark outcome on disk
                _set_trial_outcome(
                    session_dir, outcome="success", score_status="scored"
                )
    except BaseException as exc:
        # Ctrl+C / SystemExit: undeploy the lab, leave the trial incomplete for
        # --resume, and re-raise. Do not count as agent_failed.
        if _is_user_interrupt(exc):
            try:
                close_session(
                    session_id=session_id,
                    undeploy=True,
                    session_dir=session_dir,
                    status="aborted",
                )
                _mark_session_aborted(session_dir)
                print(f"cleaned up interrupted session {session_id} (lab undeployed)")
            except Exception as cleanup_error:  # noqa: BLE001 - best effort
                print(
                    f"WARNING: could not clean up interrupted session "
                    f"{session_id}: {cleanup_error}"
                )
            raise

        # Batch runs (--config / --release): post-inject failures become
        # success (valid submission), counted agent_failed, or retryable
        # endpoint_failed / infra_failed. Bare single-case CLI (no trial_id)
        # still raises so abort-on-error behavior is preserved.
        if trial_id and (gt_written or (session_dir / "ground_truth.json").is_file()):
            outcome = _finalize_post_inject_failure(
                session_id=session_id,
                session_dir=session_dir,
                result_dir=result_dir,
                error=exc,
            )
            vprint(
                verbose or not trial_id,
                f"{_BENCHMARK_DONE_PREFIX}session_id={session_id} scenario={scenario} "
                f"problem={problem} session_dir={session_dir} outcome={outcome}",
            )
            if outcome in RETRYABLE_OUTCOMES:
                # Surface as a continuing failure so retry-passes / resume see it.
                raise
            return session_id, session_dir

        _close_quietly(session_id, session_dir, status="error")
        vprint(verbose, f"cleaned up failed session {session_id} (lab undeployed)")

        def _mark_error(run_meta: dict[str, Any]) -> None:
            run_meta["status"] = "error"
            if not run_meta.get("outcome"):
                run_meta["outcome"] = "error"

        try:
            update_run_json(session_dir, _mark_error)
        except OSError:
            pass
        raise

    vprint(
        verbose or not trial_id,
        f"{_BENCHMARK_DONE_PREFIX}session_id={session_id} scenario={scenario} "
        f"problem={problem} session_dir={session_dir}",
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
    heavy_batch_size: int = 1,
    result_dir: str | None = None,
    resume: bool = True,
    session_tag: str | None = None,
    case_timeout: int = 0,
    continue_on_error: bool = False,
    retry_passes: int = 0,
    release_meta: dict | None = None,
    task_ids: list[str] | None = None,
    yes: bool = False,
    verbose: bool = False,
    output_mode: OutputMode = "human",
    plan_header: str | None = None,
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
        heavy_batch_size=heavy_batch_size,
        result_dir=result_dir,
        resume=resume,
        session_tag=session_tag,
        case_timeout=case_timeout,
        continue_on_error=continue_on_error,
        retry_passes=retry_passes,
        release_meta=release_meta,
        task_ids=task_ids,
        yes=yes,
        verbose=verbose,
        output_mode=output_mode,
        plan_header=plan_header,
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
    verbose: bool = False,
    progress: BenchmarkProgress | None = None,
) -> None:
    # Runs in-process or as a spawn worker; silence before FastMCP re-import noise.
    quiet_third_party_logging()
    row = trial.row
    run_single_case(
        problem=row["problem"],
        problems=row_problems(row),
        scenario=row["scenario"],
        topo_size=row.get("topo_size") or "",
        inject_params=row["inject"],
        expected_root_causes=(
            None
            if row.get("root_causes_status") == "unresolved"
            else row.get("root_causes")
        ),
        candidate_option_id=row.get("candidate_option_id"),
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
        **{key: row.get(key) for key in ISP_DEPLOY_KEYS},
        verbose=verbose,
        progress=progress,
        progress_label=trial.label if progress is not None else None,
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
    verbose: bool = False,
    progress: BenchmarkProgress | None = None,
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
        verbose=verbose,
    )
    if case_timeout <= 0 and not isolate:
        # In-process: Live can receive phase hooks directly.
        _run_trial(trial, progress=progress, **kwargs)
        return

    import multiprocessing

    from nika.workflows.benchmark._trial_worker import run_trial_worker

    apply_worker_warning_env()
    ctx = multiprocessing.get_context("spawn")
    # Spawn worker must not receive the Live progress object.
    proc = ctx.Process(target=run_trial_worker, kwargs={"trial": trial, **kwargs})
    proc.start()
    _register_trial_proc(proc)
    join_timeout = case_timeout if case_timeout > 0 else None
    try:
        try:
            proc.join(join_timeout)
        except KeyboardInterrupt:
            # The worker got SIGINT too; give it the same grace to undeploy.
            _terminate_active_trial_workers()
            raise
        # Parent is stopping workers (Ctrl+C / --abort-on-error); do not stamp
        # an outcome — leave the trial incomplete for --resume.
        if _interrupt_cleanup_active:
            _stop_worker(proc)
            raise KeyboardInterrupt()
        if case_timeout > 0 and proc.is_alive():
            # SIGTERM runs the worker's cleanup (sandbox, gateway, lab).
            _stop_worker(proc)
            timeout_error = RuntimeError(
                f"[{trial.trial_id}] case exceeded --case-timeout ({case_timeout}s) "
                "and was killed. Its lab may be leaked — check `nika session ps`."
            )
            _finalize_timed_out_trial(trial, result_dir=result_dir, error=timeout_error)
            raise timeout_error
        if proc.is_alive():
            _stop_worker(proc)
            raise RuntimeError(f"[{trial.trial_id}] trial worker did not exit")
        if proc.exitcode not in (0, None):
            # Worker may have finalized already; if not and GT exists, classify
            # the crash (agent never started / endpoint → retryable).
            results_root = resolve_results_root(result_dir)
            session_dir = trial_dir(results_root, trial.case_key, trial.trial_index)
            # The worker logged the real cause before dying; surface it here so
            # the run summary is actionable without opening each trial dir.
            logged = last_session_error(session_dir)
            crash_error = RuntimeError(
                f"[{trial.trial_id}] trial worker exited with code {proc.exitcode}"
                + (f": {logged}" if logged else "")
            )
            # An external watchdog SIGTERM while a model request hangs is an
            # endpoint failure: finalize it (retryable) even though the
            # worker's SIGTERM handler marked the session aborted.
            killed_mid_llm = is_signal_exit_code(
                proc.exitcode
            ) and trace_ends_in_llm_call(session_dir)
            if trial_outcome(session_dir) == "aborted" and not killed_mid_llm:
                # The worker handled SIGTERM/SIGINT and undeployed its lab;
                # leave the slot incomplete for --resume.
                raise crash_error
            if not (session_dir / "ground_truth.json").is_file() or (
                not is_valid_trial(session_dir)
                and not heal_trial_outcome(session_dir)
                and not is_finalized_failure(session_dir)
            ):
                _finalize_timed_out_trial(
                    trial, result_dir=result_dir, error=crash_error
                )
            raise crash_error
    finally:
        _unregister_trial_proc(proc)


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
    max_workers: int | None = None,
    heavy_batch_size: int = 1,
    verbose: bool = False,
    progress: BenchmarkProgress | None = None,
    on_trial_finished: Any | None = None,
) -> list[str]:
    """Run trials with a concurrency cap (sliding window).

    ``max_workers`` limits how many light trials run at once and
    ``heavy_batch_size`` how many heavy trials (Containerlab, k8s/llmd/XRd,
    topo_size ``l``) run at once. When one finishes, the next pending trial
    starts immediately — slots are not held empty until a fixed wave drains.
    Light and heavy trials never run at the same time; with the default
    ``heavy_batch_size=1`` each heavy trial has the host to itself. Parallel
    work uses spawn processes for isolation.
    """
    failures: list[str] = []
    if not trials_batch:
        return failures
    limits = tier_limits(
        batch_size=max_workers or len(trials_batch),
        heavy_batch_size=heavy_batch_size,
    )
    workers = max(1, min(max(limits.values()), len(trials_batch)))
    # Parallel Kathara/MCP work is not safe on shared in-process clients.
    isolate = workers > 1
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
        verbose=verbose,
    )
    results_root = resolve_results_root(result_dir)

    def _begin_progress(trial: Trial) -> None:
        if progress is not None:
            progress.start_trials([trial.label])
            progress.attach_session(
                trial.label,
                trial_dir(results_root, trial.case_key, trial.trial_index),
            )
            # Spawn workers cannot push phases; start at deploy then agent.
            progress.set_phase(trial.label, "deploy")
            if case_timeout > 0 or isolate:
                progress.set_phase(trial.label, "agent")
        elif verbose:
            if workers == 1 and case_timeout <= 0:
                print(f"{trial.label} {trial.trial_id} running")
            else:
                print(f"[batch] start {trial.label} {trial.trial_id}")

    def _on_finished(trial: Trial, *, failed: bool = False) -> None:
        if progress is None:
            return
        session_dir = trial_dir(results_root, trial.case_key, trial.trial_index)
        # Only advance the Live counter for counted slots. Incomplete failures
        # stay retryable and must not inflate completed / success stats.
        if is_valid_trial(session_dir) or heal_trial_outcome(session_dir):
            counted_fail = failed or trial_outcome(session_dir) == "agent_failed"
            progress.finish_trial(
                trial.label,
                metrics=read_trial_metrics(session_dir),
                failed=counted_fail,
            )
        else:
            progress.abandon_trial(trial.label)

    def _report_failure(trial: Trial, error: Exception) -> None:
        msg = f"TRIAL FAILED (continuing): [{trial.trial_id}] {error}"
        if progress is not None:
            progress.log(msg)
        else:
            print(msg)
        failures.append(f"[{trial.trial_id}] {error}")

    def _notify_finished(trial: Trial) -> None:
        if on_trial_finished is not None:
            on_trial_finished(trial)

    def _run_one(trial: Trial) -> None:
        _begin_progress(trial)
        _run_trial_with_timeout(trial, progress=progress, **shared)

    def _abort_in_flight() -> None:
        # --abort-on-error: stop peers through the SIGTERM grace path so they
        # undeploy their labs, then sweep sessions still running under this run.
        cleanup_benchmark_interrupt(result_dir, signal_workers=True)

    # Ctrl+C: re-raise; ``run_benchmark_trials`` runs cleanup exactly once.
    if workers == 1:
        for trial in trials_batch:
            try:
                _run_one(trial)
                _on_finished(trial)
                _notify_finished(trial)
            except Exception as e:  # noqa: BLE001
                _on_finished(trial, failed=True)
                _notify_finished(trial)
                if not continue_on_error:
                    raise
                _report_failure(trial, e)
        return failures

    if verbose and progress is None:
        print(
            f"[batch] running {len(trials_batch)} trial(s) "
            f"(max {limits['light']} light / {limits['heavy']} heavy concurrent)"
        )

    # Avoid ``with ThreadPoolExecutor``: on Ctrl+C its __exit__ joins worker
    # threads that are blocked in Process.join, hanging forever. Shut down
    # without waiting after terminating child processes.
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = list(trials_batch)
    # future -> (trial, resource_class)
    futures: dict[Any, tuple[Trial, str]] = {}
    in_flight: dict[str, int] = {}

    def _admit_available() -> None:
        while len(futures) < workers and pending:
            index = pick_admissible(pending, in_flight=in_flight, limits=limits)
            if index is None:
                break
            trial = pending.pop(index)
            cls = resource_class(trial)
            in_flight[cls] = in_flight.get(cls, 0) + 1
            future = pool.submit(_run_one, trial)
            futures[future] = (trial, cls)

    try:
        _admit_available()
        while futures:
            for future in as_completed(futures):
                trial, cls = futures.pop(future)
                in_flight[cls] = max(0, in_flight.get(cls, 0) - 1)
                try:
                    future.result()
                    _on_finished(trial)
                    _notify_finished(trial)
                except Exception as e:  # noqa: BLE001
                    _on_finished(trial, failed=True)
                    _notify_finished(trial)
                    if not continue_on_error:
                        _abort_in_flight()
                        raise
                    _report_failure(trial, e)
                _admit_available()
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
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
    heavy_batch_size: int = 1,
    result_dir: str | None = None,
    resume: bool = True,
    session_tag: str | None = None,
    case_timeout: int = 0,
    continue_on_error: bool = False,
    retry_passes: int = 0,
    release_meta: dict | None = None,
    task_ids: list[str] | None = None,
    yes: bool = False,
    verbose: bool = False,
    output_mode: OutputMode = "human",
    plan_header: str | None = None,
    job: dict[str, Any] | None = None,
    check_images: bool = True,
) -> None:
    """Run cases × ``n_trials`` under ``{result_dir}/trials/`` (shared batch kernel).

    ``job`` is a release run config already checked against any existing one
    (``run_benchmark_from_release``). Without it (``--config``), an ad-hoc run
    config is built and checked the same way, so ``--resume`` refuses to mix
    agents/models/timeouts in one result dir. Either is written only after the
    user confirms the plan.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if heavy_batch_size < 1:
        raise ValueError("heavy_batch_size must be >= 1")
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if retry_passes < 0:
        raise ValueError("retry_passes must be >= 0")
    if retry_passes and not continue_on_error:
        continue_on_error = True

    global _interrupt_cleanup_active
    _interrupt_cleanup_active = False

    # Quiet MCP/httpx before lab deploy and agent work (default CLI UX).
    quiet_third_party_logging()

    rows = load_benchmark_input(benchmark_file)
    if not rows:
        print(f"No benchmark rows found in {benchmark_file}")
        return
    release_meta = dict(release_meta or {})

    trials = expand_trials(rows, n_trials)
    if task_ids:
        trials = select_trials(trials, task_ids)
    case_count = len({trial.case_key for trial in trials})

    results_root = resolve_results_root(result_dir)
    is_release = job is not None
    if job is None:
        job = merge_run_config(
            existing=load_run_config(results_root),
            proposed={
                "benchmark_id": None,
                "version": None,
                "split": None,
                "benchmark_ref": str(benchmark_file),
                **build_run_identity(
                    agent_type=agent_type,
                    model=model,
                    llm_provider=llm_provider,
                    max_steps=max_steps,
                    n_trials=n_trials,
                    case_timeout_sec=case_timeout,
                    official=False,
                ),
            },
        )
        job["case_count"] = len(rows)
        if task_ids:
            job["task_ids"] = list(task_ids)
            job["planned_trial_count"] = len(trials)
        else:
            job.pop("task_ids", None)
            job.pop("planned_trial_count", None)
    run_id = None
    if release_meta:
        run_id = release_meta.get("run_id") or release_meta.get("job_id")
    inspect_url: str | None = None

    def _emit_report() -> None:
        """Print the visual summary for the finished run.

        Best-effort: a completed run must not fail because reporting did.
        """
        try:
            from rich.console import Console

            from nika.utils.session_artifacts import iter_session_dirs
            from nika.workflows.eval.render import render_summary_report
            from nika.workflows.eval.report import build_summary_report

            report = build_summary_report(
                iter_session_dirs(str(results_root)),
                result_dir=results_root,
                n_trials_expected=len(trials),
            )
            if report.n_trials_present:
                summary_console = (
                    Console(force_terminal=False, no_color=True)
                    if output_mode == "agent"
                    else None
                )
                render_summary_report(
                    report,
                    metric=report.primary_metric,
                    console=summary_console,
                )
        except Exception as report_error:  # noqa: BLE001 - reporting is advisory
            print(f"WARNING: could not render run summary: {report_error}")
        print_inspect_hint(results_root, url=inspect_url, output_mode=output_mode)
        print_deferred_warnings(output_mode=output_mode)

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

    def _run_pending(
        pending: list[int], *, progress: BenchmarkProgress | None
    ) -> list[str]:
        # Count once per pass, then update per finished trial: no full rescan,
        # and in-flight trial slots are never read-modify-written here.
        pending_set = set(pending)
        completed_ids = {
            trial.trial_id
            for index, trial in enumerate(trials)
            if index not in pending_set
            and is_valid_trial(
                trial_dir(results_root, trial.case_key, trial.trial_index)
            )
        }

        def _refresh_after_trial(trial: Trial) -> None:
            if not run_id:
                return
            if is_valid_trial(
                trial_dir(results_root, trial.case_key, trial.trial_index)
            ):
                completed_ids.add(trial.trial_id)
            completed = len(completed_ids)
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

        batch = [trials[index] for index in pending]
        return _run_trials_batch(
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
            max_workers=batch_size,
            heavy_batch_size=heavy_batch_size,
            verbose=verbose,
            progress=progress,
            on_trial_finished=_refresh_after_trial,
        )

    # Preflight plan only — do not clear/cleanup slots until the user confirms.
    # announce=False: the Plan below owns the resume summary (no skip/path spam).
    _, plan_pending = scan_trials(
        trials=trials,
        result_dir=results_root,
        resume=resume,
        verbose=verbose,
        announce=False,
        mutate=False,
    )
    pending_set = set(plan_pending)
    print_run_plan(
        RunPlan(
            total_trials=len(trials),
            pending_count=len(plan_pending),
            skipped_count=len(trials) - len(plan_pending),
            agent_type=agent_type,
            model=model,
            result_dir=str(results_root),
            pending_labels=[trials[i].label for i in plan_pending],
            skipped_labels=[
                trials[i].label for i in range(len(trials)) if i not in pending_set
            ],
            header=plan_header,
            batch_size=batch_size,
            heavy_batch_size=heavy_batch_size,
            case_count=case_count,
            n_trials=n_trials,
        ),
        output_mode=output_mode,
        **({"max_labels": 10_000} if verbose else {}),
    )
    if not plan_pending:
        _finish_progress([])
        _emit_report()
        return
    if not confirm_run(yes=yes):
        print("Aborted.")
        return

    # Confirmed: persist the run config, then pull sandbox images once per job
    # before any case/lab deploy so parallel trials do not race on the first
    # ``sbx create`` Docker Hub pull.
    if is_release:
        write_job_metadata(results_root, job)
    else:
        write_json_atomic(results_root / RUN_CONFIG_FILENAME, job, sort_keys=True)
    if run_id:
        write_progress(
            str(run_id),
            result_dir=results_root,
            status="running",
            total_trials=len(trials),
            completed_trials=len(trials) - len(plan_pending),
            pending_trials=len(plan_pending),
            benchmark_id=job.get("benchmark_id"),
            version=job.get("version"),
            agent_type=job.get("agent_type"),
            model=job.get("model"),
        )
    if check_images:
        from agent.sandbox.sbx.images import ensure_sbx_template_images

        ensure_sbx_template_images([agent_type])

    # Confirmed: mutate slots (clear --no-resume / clean incomplete) then run.
    _, initial_pending = scan_trials(
        trials=trials,
        result_dir=results_root,
        resume=resume,
        verbose=verbose,
        announce=False,
        mutate=True,
    )
    _refresh_progress(initial_pending)
    if not initial_pending:
        _finish_progress([])
        _emit_report()
        return

    failures: list[str] = []
    previous_pending: int | None = None
    try:
        inspect_url = _maybe_start_inspect(results_root, output_mode=output_mode)
        with BenchmarkProgress(
            len(trials),
            initial_completed=len(trials) - len(initial_pending),
            agent_type=agent_type,
            model=model,
            case_count=case_count,
            n_trials=n_trials,
            inspect_url=inspect_url,
            output_mode=output_mode,
        ) as progress:
            for attempt in range(retry_passes + 1):
                if attempt == 0:
                    pending = initial_pending
                else:
                    _root, pending = scan_trials(
                        trials=trials,
                        result_dir=results_root,
                        resume=True,
                        verbose=verbose,
                        announce=False,
                    )
                    progress.set_completed(len(trials) - len(pending))
                    _refresh_progress(pending)
                if not pending:
                    if attempt > 0:
                        print("\nAll trials completed after retries.")
                    _finish_progress([])
                    break
                if attempt > 0:
                    if (
                        previous_pending is not None
                        and len(pending) >= previous_pending
                    ):
                        print(
                            f"\nRetry made no progress ({len(pending)} trial(s) still "
                            "incomplete); stopping retries."
                        )
                        break
                    print(
                        f"\n[retry {attempt}/{retry_passes}] retrying "
                        f"{len(pending)} incomplete trial(s)"
                    )
                previous_pending = len(pending)
                failures = _run_pending(pending, progress=progress)
                # agent_failed trials count as complete; only incomplete remain.
                # Read-only: a failed slot keeps its nika.jsonl so the user can
                # see why it died. The next pass's scan clears it before re-running.
                _root, still_pending = scan_trials(
                    trials=trials,
                    result_dir=results_root,
                    resume=True,
                    verbose=verbose,
                    announce=False,
                    mutate=False,
                )
                _refresh_progress(still_pending)
                if not still_pending:
                    if attempt > 0:
                        print("\nAll trials completed after retries.")
                    _finish_progress([])
                    break
                if not failures and still_pending:
                    # agent_failed trials count as complete; remaining pending
                    # means incomplete artifacts after the pass.
                    failures = [
                        f"incomplete trial {trials[i].trial_id}" for i in still_pending
                    ]
    except KeyboardInterrupt:
        print(
            "\nInterrupted — cleaning up running benchmark sessions "
            f"under {results_root}…"
        )
        cleanup_benchmark_interrupt(results_root)
        if run_id:
            try:
                _root, pending = scan_trials(
                    trials=trials,
                    result_dir=results_root,
                    resume=True,
                    verbose=verbose,
                    announce=False,
                    mutate=False,
                )
                update_progress_from_scan(
                    str(run_id),
                    result_dir=results_root,
                    total_trials=len(trials),
                    pending=pending,
                    status="aborted",
                    release_meta=release_meta,
                )
            except Exception:  # noqa: BLE001 - progress is advisory
                pass
        raise

    _, final_pending = scan_trials(
        trials=trials,
        result_dir=results_root,
        resume=True,
        verbose=verbose,
        announce=False,
        mutate=False,
    )
    _finish_progress(final_pending)

    if failures:
        print(
            f"\n{len(failures)} trial(s) still FAILED "
            "(re-run the same command with --resume to retry only incomplete trials):"
        )
        for message in failures:
            print(f"  - {message.splitlines()[0]}")

    _emit_report()


def run_benchmark_from_release(
    release_ref: str,
    agent_type: str,
    llm_provider: str | None,
    model: str | None,
    max_steps: int | None,
    *,
    split: SplitName | str = "test",
    batch_size: int = 1,
    heavy_batch_size: int = 1,
    result_dir: str | None = None,
    resume: bool = True,
    session_tag: str | None = None,
    case_timeout: int | None = None,
    continue_on_error: bool = True,
    retry_passes: int = 0,
    check_images: bool = True,
    release: BenchmarkRelease | None = None,
    task_ids: list[str] | None = None,
    yes: bool = False,
    verbose: bool = False,
    output_mode: OutputMode = "human",
) -> None:
    """Run a frozen ``nika-bench`` release split after preflight validation.

    Official release runs default to ``continue_on_error=True`` so a single
    trial failure does not abort the job; pass False (CLI ``--abort-on-error``)
    to stop on the first error.
    """
    resolved_split = normalize_split(split, default="test")
    resolved = release or load_release(release_ref, split=resolved_split)
    if resolved.split != resolved_split:
        resolved = load_release(release_ref, split=resolved_split)
    n_trials = resolved.n_trials
    # Validate --task-id before preflight / image pulls so bad selectors fail fast.
    planned: list[Trial] | None = None
    if task_ids:
        planned = select_trials(expand_trials(resolved.cases, n_trials), task_ids)
    preflight_release(resolved, check_images=check_images)

    # Timeout is operational (run config / CLI), not a release pin.
    if case_timeout is None:
        from nika.run_config.schema import BenchmarkSettings

        case_timeout = int(BenchmarkSettings.model_fields["case_timeout_sec"].default)
    effective_timeout = int(case_timeout)
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
    if planned is not None:
        total_trials = len(planned)
        job["task_ids"] = list(task_ids or [])
        job["planned_trial_count"] = total_trials
        case_count = len({trial.case_key for trial in planned})
        scope = f"{case_count} cases, {total_trials} trials"
    else:
        # A full run supersedes an earlier --task-id scope; a stale planned
        # count would shrink offline score denominators.
        job.pop("task_ids", None)
        job.pop("planned_trial_count", None)
        total_trials = int(resolved.case_count) * int(n_trials)
        scope = f"{resolved.case_count} cases × {n_trials} trials"
    job_path = results_root / RUN_CONFIG_FILENAME
    plan_header = (
        f"Release {resolved.ref} split={resolved.split} "
        f"({scope}, "
        f"official={official}, continue_on_error={continue_on_error}) → {job_path}"
    )

    run_benchmark_trials(
        benchmark_file=str(resolved.cases_path),
        agent_type=agent_type,
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        n_trials=n_trials,
        batch_size=batch_size,
        heavy_batch_size=heavy_batch_size,
        result_dir=str(results_root),
        resume=resume,
        session_tag=session_tag,
        case_timeout=effective_timeout,
        continue_on_error=continue_on_error,
        retry_passes=retry_passes,
        release_meta=job,
        task_ids=task_ids,
        yes=yes,
        verbose=verbose,
        output_mode=output_mode,
        plan_header=plan_header,
        job=job,
        check_images=check_images,
    )
