"""Session evaluation: numeric metrics and LLM judge on closed sessions."""

import json
import os
from pathlib import Path

from nika.config import resolve_results_root
from nika.evaluator.llm_judge import LLMJudge
from nika.evaluator.result_log import EVAL_METRICS_FILENAME, MESSAGES_FILENAME
from nika.evaluator.trace_parser import AgentTraceParser
from nika.evaluator.scoring import (
    GradingError,
    score_detection,
    score_rca_v2,
)
from nika.evaluator.score_status import (
    infer_score_status_from_artifacts,
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
    gt_present = has_ground_truth if has_ground_truth is not None else gt is not None
    if submission is None:
        status = resolve_score_status(
            has_submission=False,
            infra_evidence=infra_evidence,
            has_ground_truth=gt_present,
        )
        scores = scores_for_status(status)  # type: ignore[arg-type]
    else:
        if not gt_present or gt is None:
            status = "grading_error"
            scores = dict(null_scores())
            payload = {
                **scores,
                "in_tokens": trace_metrics.get("in_tokens"),
                "out_tokens": trace_metrics.get("out_tokens"),
                "steps": trace_metrics.get("steps"),
                "tool_calls": trace_metrics.get("tool_calls"),
                "tool_errors": trace_metrics.get("tool_errors"),
            }
            return payload, status
        try:
            scores = generic_eval(gt, submission)
            status = "scored"
        except GradingError:
            status = "grading_error"
            scores = dict(null_scores())
        except Exception:
            status = "grading_error"
            scores = dict(null_scores())

    payload = {
        **scores,
        "in_tokens": trace_metrics.get("in_tokens"),
        "out_tokens": trace_metrics.get("out_tokens"),
        "steps": trace_metrics.get("steps"),
        "tool_calls": trace_metrics.get("tool_calls"),
        "tool_errors": trace_metrics.get("tool_errors"),
    }
    return payload, status


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
    session = Session()
    session.load_closed_session(
        session_id=session_id, result_dir=result_dir, session_dir=session_dir
    )
    bind_session_dir(session.session_dir)

    session_dir = Path(session.session_dir)
    submission_path = session_dir / "submission.json"
    has_submission = submission_path.is_file()
    submission = None
    if has_submission:
        try:
            submission = json.loads(submission_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt submission file: treat as present but grading will fail.
            submission = {}
            has_submission = True

    if not has_submission:
        logger.error(f"Submission file not found: {submission_path}")

    run_meta: dict = {}
    try:
        raw = json.loads((session_dir / RUN_FILENAME).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            run_meta = raw
    except (json.JSONDecodeError, OSError):
        pass

    effective_infra = infra_evidence or (
        not has_submission
        and infer_score_status_from_artifacts(
            has_submission=False,
            run_meta=run_meta,
            has_ground_truth=(session_dir / "ground_truth.json").is_file(),
        )
        == "infra_error"
    )

    gt_path = session_dir / "ground_truth.json"
    try:
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        if not isinstance(gt, dict):
            raise GradingError("ground_truth.json is not an object")
    except (json.JSONDecodeError, OSError, GradingError) as exc:
        if has_submission:
            status = "grading_error"
            scores = dict(null_scores())
        else:
            status = resolve_score_status(
                has_submission=False,
                infra_evidence=effective_infra,
                has_ground_truth=False,
            )
            scores = scores_for_status(status)  # type: ignore[arg-type]
        payload = {
            **scores,
            "in_tokens": None,
            "out_tokens": None,
            "steps": None,
            "tool_calls": None,
            "tool_errors": None,
        }
        out_path = session_dir / EVAL_METRICS_FILENAME
        write_json_atomic(out_path, payload)
        _stamp_score_status(session_dir, status)
        session.update_run_meta("eval_metrics", payload)
        session.update_run_meta("score_status", status)
        logger.error(f"Ground truth unreadable for {session_id}: {exc}")
        return

    trace_path = os.path.join(session.session_dir, MESSAGES_FILENAME)
    try:
        trace_metrics = AgentTraceParser(trace_path=trace_path).parse_trace()
    except Exception:  # noqa: BLE001 - traces optional for scoring
        trace_metrics = {}
    payload, status = build_eval_metrics_payload(
        gt=gt,
        submission=submission,
        trace_metrics=trace_metrics,
        infra_evidence=effective_infra,
    )
    out_path = session_dir / EVAL_METRICS_FILENAME
    write_json_atomic(out_path, payload)
    _stamp_score_status(session_dir, status)
    session.update_run_meta("eval_metrics", payload)
    session.update_run_meta("score_status", status)
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
