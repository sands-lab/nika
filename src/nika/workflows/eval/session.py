"""Session evaluation: numeric metrics and LLM judge on closed sessions."""

import json
import os
from pathlib import Path

from nika.benchmark_engine.protocol import LifecycleProtocol, Scores, TrialHandle
from nika.config import resolve_results_root
from nika.evaluator.llm_judge import LLMJudge
from nika.evaluator.validity import detect_trial_validity
from nika.evaluator.result_log import (
    EVAL_METRICS_FILENAME,
    GROUND_TRUTH_FILENAME,
    MESSAGES_FILENAME,
    SUBMISSION_FILENAME,
)
from nika.evaluator.trace_parser import AgentTraceParser
from nika.evaluator.scoring import (
    SCORING_ID,
    GradingError,
    score_detection,
    score_rca_v2,
)
from nika.evaluator.score_status import (
    is_infra_error_evidence,
    null_scores,
    resolve_score_status,
    scores_for_status,
)
from nika.utils.logger import bind_session_dir, log_event, system_logger
from nika.utils.session import Session
from nika.utils.session_artifacts import write_json_atomic
from nika.utils.session_artifacts import (
    RUN_FILENAME,
    is_finished_session,
    iter_session_dirs,
)
from nika.utils.session_store import SessionStore
from nika.workflows.session.close import close_session

logger = system_logger

# Trace statistics the frontend adds to the engine's scores.
TRACE_KEYS = ("in_tokens", "out_tokens", "steps", "tool_calls", "tool_errors")


def _format_judge_ground_truth(gt: dict) -> str:
    causes = gt.get("root_causes") or []
    if not causes:
        return "No structured root causes (healthy or unlabeled session)."
    lines = ["Structured root causes (resource + fault_type):"]
    for item in causes:
        resource = item.get("resource") or {}
        resource_id = item.get("resource_id") or resource.get("id") or resource
        lines.append(f"- {resource_id} type={item.get('fault_type')}")
    return "\n".join(lines)


def _session_is_still_running(session_id: str) -> bool:
    try:
        return SessionStore().get_session(session_id).get("status") == "running"
    except FileNotFoundError:
        return False


def _iter_eval_session_ids(
    *,
    session_id: str | None = None,
    result_dir: str | Path | None = None,
) -> list[str]:
    """Return session ids to evaluate under *result_dir* (or the default results root)."""
    if session_id is not None:
        return [session_id]

    results_root = resolve_results_root(result_dir)
    candidates: list[str] = []
    for session_dir in iter_session_dirs(results_root):
        run_meta = json.loads((session_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        if not is_finished_session(run_meta):
            continue
        sid = run_meta.get("session_id") or session_dir.name
        if _session_is_still_running(sid):
            continue
        candidates.append(sid)

    if not candidates:
        raise FileNotFoundError(
            f"No closed session found under {results_root}/. "
            "Close a session with `nika session close` first."
        )
    if result_dir is None and len(candidates) > 1:
        raise ValueError(
            "Multiple closed sessions found under results/. Please pass --session_id to select one."
        )
    return candidates


def generic_eval(gt, submission):
    """Score detection and pair-based RCA from structured ``gt`` and ``submission``."""
    detection_score = score_detection(submission, gt)
    return {
        "detection_score": detection_score,
        **score_rca_v2(submission, gt),
    }


def _grade(
    *,
    gt: dict | None,
    submission: dict | None,
    infra_evidence: bool = False,
    has_ground_truth: bool | None = None,
) -> tuple[dict, str, str | None]:
    """Return ``(scores, score_status, error)`` for one answer."""
    gt_present = has_ground_truth if has_ground_truth is not None else gt is not None
    if submission is None:
        status = resolve_score_status(
            has_submission=False,
            infra_evidence=infra_evidence,
            has_ground_truth=gt_present,
        )
        error = None
        if status == "infra_error":
            error = (
                "no ground truth and no submission"
                if not gt_present
                else "infrastructure failure before the agent submitted"
            )
        return scores_for_status(status), status, error  # type: ignore[arg-type]
    if not gt_present or gt is None:
        return dict(null_scores()), "grading_error", "no ground truth"
    try:
        return generic_eval(gt, submission), "scored", None
    except Exception as exc:  # noqa: BLE001 - recorded as grading_error
        return dict(null_scores()), "grading_error", f"{type(exc).__name__}: {exc}"


def _trace_columns(trace_metrics: dict) -> dict:
    return {key: trace_metrics.get(key) for key in TRACE_KEYS}


def build_eval_metrics_payload(
    *,
    gt: dict | None,
    submission: dict | None,
    trace_metrics: dict,
    infra_evidence: bool = False,
    has_ground_truth: bool | None = None,
) -> tuple[dict, str]:
    """Build metrics payload and resolved ``score_status``.

    Returns ``(payload, score_status)``. Score columns are ``[0, 1]`` or ``null``;
    never ``-1``.
    """
    scores, status, _error = _grade(
        gt=gt,
        submission=submission,
        infra_evidence=infra_evidence,
        has_ground_truth=has_ground_truth,
    )
    return {**scores, **_trace_columns(trace_metrics)}, status


def _read_ground_truth(session_dir: Path) -> dict:
    """Return the trial's ground truth; raise when it is missing or malformed."""
    gt = json.loads((session_dir / GROUND_TRUTH_FILENAME).read_text(encoding="utf-8"))
    if not isinstance(gt, dict):
        raise GradingError("ground_truth.json is not an object")
    return gt


def _ground_truth_readable(session_dir: Path) -> bool:
    try:
        _read_ground_truth(session_dir)
    except (json.JSONDecodeError, OSError, GradingError):
        return False
    return True


def grade_trial(trial_dir: str | Path, *, infra_evidence: bool = False) -> Scores:
    """Grade ``submission.json`` against ``ground_truth.json`` in *trial_dir*.

    This is the benchmark engine's half of scoring: it reads only those two
    files and the caller's *infra_evidence* flag, and writes nothing.
    """
    trial_dir = Path(trial_dir)
    submission_path = trial_dir / SUBMISSION_FILENAME
    submission = None
    if submission_path.is_file():
        try:
            submission = json.loads(submission_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt submission file: present, but grading will fail.
            submission = {}
    else:
        logger.error(f"Submission file not found: {submission_path}")

    try:
        gt = _read_ground_truth(trial_dir)
    except (json.JSONDecodeError, OSError, GradingError) as exc:
        logger.error(f"Ground truth unreadable in {trial_dir}: {exc}")
        scores, status, _error = _grade(
            gt=None,
            submission=submission,
            infra_evidence=infra_evidence,
            has_ground_truth=False,
        )
        error = f"ground truth unreadable: {exc}"
    else:
        scores, status, error = _grade(
            gt=gt, submission=submission, infra_evidence=infra_evidence
        )
    return Scores(status=status, scoring_id=SCORING_ID, metrics=scores, error=error)


def frontend_infra_evidence(session_dir: str | Path, infra_evidence: bool) -> bool:
    """Infrastructure evidence the frontend sends with ``score``.

    That is the caller's flag, or an infrastructure failure recorded as the
    trial's ``agent_error`` in ``run.json``.
    """
    if infra_evidence:
        return True
    try:
        run_meta = json.loads(
            (Path(session_dir) / RUN_FILENAME).read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError):
        return False
    error = run_meta.get("agent_error") if isinstance(run_meta, dict) else None
    return is_infra_error_evidence(error if isinstance(error, str) else None)


def eval_metrics_payload(scores: Scores, session_dir: str | Path) -> dict:
    """Build ``eval_metrics.json`` from the engine's *scores* and the trial's traces.

    The frontend adds token, step, and tool-call counts and validity flags.
    A trial whose ground truth is unreadable keeps null counts and no flags.
    """
    session_dir = Path(session_dir)
    if not _ground_truth_readable(session_dir):
        return {**scores.metrics, **_trace_columns({})}
    try:
        trace_metrics = AgentTraceParser(
            trace_path=str(session_dir / MESSAGES_FILENAME)
        ).parse_trace()
    except Exception:  # noqa: BLE001 - traces optional for scoring
        trace_metrics = {}
    payload = {**scores.metrics, **_trace_columns(trace_metrics)}
    payload["validity_flags"] = detect_trial_validity(
        session_dir, score_status=scores.status
    ).flags
    return payload


def _stamp_score_status(session_dir: Path, score_status: str) -> None:
    run_path = Path(session_dir) / RUN_FILENAME
    if not run_path.is_file():
        return
    try:
        run_meta = json.loads(run_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(run_meta, dict):
        return
    run_meta["score_status"] = score_status
    run_path.write_text(json.dumps(run_meta, indent=2, default=str), encoding="utf-8")


def _write_eval_metrics(session: Session, scores: Scores) -> None:
    """Write ``eval_metrics.json`` and stamp the trial's ``run.json``."""
    session_dir = Path(session.session_dir)
    payload = eval_metrics_payload(scores, session_dir)
    out_path = session_dir / EVAL_METRICS_FILENAME
    write_json_atomic(out_path, payload)
    _stamp_score_status(session_dir, scores.status)
    session.update_run_meta("eval_metrics", payload)
    session.update_run_meta("score_status", scores.status)
    log_event(
        "eval_metrics_saved",
        f"Wrote numeric eval metrics to {out_path}",
        session_id=session.session_id,
    )
    log_event(
        "eval_publish",
        f"Published evaluation for session {session.session_id} (scenario {session.scenario_name}).",
        session_id=session.session_id,
        scenario=session.scenario_name,
    )


def _closed_session(
    session_id: str,
    *,
    result_dir: str | Path | None = None,
    session_dir: str | Path | None = None,
) -> Session:
    session = Session()
    session.load_closed_session(
        session_id=session_id, result_dir=result_dir, session_dir=session_dir
    )
    bind_session_dir(session.session_dir)
    return session


def score_trial(
    engine: LifecycleProtocol,
    handle: TrialHandle,
    *,
    result_dir: str | Path | None = None,
    infra_evidence: bool = False,
) -> Scores:
    """Score a closed trial on *engine* and write its ``eval_metrics.json``.

    The engine grades the answer; the frontend adds the trace statistics and
    validity flags and is the only writer of the file.
    """
    session = _closed_session(
        handle.session_id, result_dir=result_dir, session_dir=handle.trial_path
    )
    session_dir = Path(session.session_dir)
    scores = engine.score(
        handle, infra_evidence=frontend_infra_evidence(session_dir, infra_evidence)
    )
    _write_eval_metrics(session, scores)
    return scores


def run_eval_metrics(
    *,
    session_id: str | None = None,
    result_dir: str | Path | None = None,
    session_dir: str | Path | None = None,
    infra_evidence: bool = False,
) -> None:
    """Compute rule-based scores and trace stats; write ``eval_metrics.json`` under each session dir."""
    for sid in _iter_eval_session_ids(session_id=session_id, result_dir=result_dir):
        _run_eval_metrics_one(
            session_id=sid,
            result_dir=result_dir,
            session_dir=session_dir if session_id is not None else None,
            infra_evidence=infra_evidence,
        )


def _run_eval_metrics_one(
    *,
    session_id: str,
    result_dir: str | Path | None = None,
    session_dir: str | Path | None = None,
    infra_evidence: bool = False,
) -> None:
    """Regrade one closed trial with this NIKA's scoring code."""
    session = _closed_session(
        session_id, result_dir=result_dir, session_dir=session_dir
    )
    session_dir = Path(session.session_dir)
    scores = grade_trial(
        session_dir,
        infra_evidence=frontend_infra_evidence(session_dir, infra_evidence),
    )
    _write_eval_metrics(session, scores)


def run_llm_judge(
    judge_llm_provider: str,
    judge_model: str,
    *,
    session_id: str | None = None,
    result_dir: str | Path | None = None,
) -> None:
    """Run LLM-as-judge only; writes ``llm_judge.json`` under each selected session dir."""
    for sid in _iter_eval_session_ids(session_id=session_id, result_dir=result_dir):
        _run_llm_judge_one(
            judge_llm_provider,
            judge_model,
            session_id=sid,
            result_dir=result_dir,
        )


def _run_llm_judge_one(
    judge_llm_provider: str,
    judge_model: str,
    *,
    session_id: str,
    result_dir: str | Path | None = None,
) -> None:
    session = Session()
    session.load_closed_session(session_id=session_id, result_dir=result_dir)
    bind_session_dir(session.session_dir)

    gt_path = Path(session.session_dir) / "ground_truth.json"
    gt = json.loads(gt_path.read_text())

    trace_path = os.path.join(session.session_dir, MESSAGES_FILENAME)
    logger.info(f"Evaluating session {session.session_id} using LLM-as-Judge.")

    llm_judge = LLMJudge(judge_llm_provider=judge_llm_provider, judge_model=judge_model)
    llm_judge.evaluate_agent(
        ground_truth=_format_judge_ground_truth(gt),
        trace_path=trace_path,
        save_path=f"{session.session_dir}/llm_judge.json",
    )
    judge_path = Path(session.session_dir) / "llm_judge.json"
    if judge_path.exists():
        session.update_run_meta(
            "llm_judge", json.loads(judge_path.read_text(encoding="utf-8"))
        )


def eval_results(
    *,
    destroy_env: bool = True,
    session_id: str | None = None,
) -> None:
    """Close the session, then write rule-based ``eval_metrics.json``.

    LLM judge and CSV summary are offline steps via ``nika eval judge`` /
    ``nika eval summary``.
    """
    resolved_session_id = session_id
    try:
        session = Session()
        session.load_running_session(session_id=session_id)
        resolved_session_id = session.session_id
        close_session(session_id=resolved_session_id, undeploy=destroy_env)
    except FileNotFoundError:
        # Session may already have been closed by another process while a long
        # agent run was still in flight; evaluate from results artifacts.
        if resolved_session_id is None:
            raise
    run_eval_metrics(session_id=resolved_session_id)
