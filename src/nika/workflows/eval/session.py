"""Session evaluation: numeric metrics and LLM judge on closed sessions."""

import json
import os
import textwrap
from pathlib import Path

from nika.config import resolve_results_root
from nika.evaluator.llm_judge import LLMJudge
from nika.evaluator.result_log import EVAL_METRICS_FILENAME, MESSAGES_FILENAME
from nika.evaluator.trace_parser import AgentTraceParser
from nika.evaluator.scoring import (
    score_detection,
    score_localization,
    score_rca,
)
from nika.utils.logger import bind_session_dir, log_event, system_logger
from nika.utils.session import Session
from nika.utils.session_artifacts import (
    RUN_FILENAME,
    is_finished_session,
    iter_session_dirs,
)
from nika.utils.session_store import SessionStore
from nika.workflows.session.close import close_session

logger = system_logger


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
    """Score detection, localization, and RCA from structured ``gt`` and ``submission``."""
    detection_score = score_detection(submission, gt)
    loc_acc, loc_prec, loc_rec, loc_f1 = score_localization(submission, gt)
    rca_acc, rca_prec, rca_rec, rca_f1 = score_rca(submission, gt)

    return (
        detection_score,
        loc_acc,
        loc_prec,
        loc_rec,
        loc_f1,
        rca_acc,
        rca_prec,
        rca_rec,
        rca_f1,
    )


def run_eval_metrics(
    *,
    session_id: str | None = None,
    result_dir: str | Path | None = None,
) -> None:
    """Compute rule-based scores and trace stats; write ``eval_metrics.json`` under each session dir."""
    for sid in _iter_eval_session_ids(session_id=session_id, result_dir=result_dir):
        _run_eval_metrics_one(session_id=sid, result_dir=result_dir)


def _run_eval_metrics_one(
    *, session_id: str, result_dir: str | Path | None = None
) -> None:
    session = Session()
    session.load_closed_session(session_id=session_id, result_dir=result_dir)
    bind_session_dir(session.session_dir)

    gt_path = Path(session.session_dir) / "ground_truth.json"
    gt = json.loads(gt_path.read_text())

    submission_path = Path(session.session_dir) / "submission.json"
    if submission_path.exists():
        submission = json.loads(submission_path.read_text())
        (
            detection_score,
            loc_acc,
            loc_prec,
            loc_rec,
            loc_f1,
            rca_acc,
            rca_prec,
            rca_rec,
            rca_f1,
        ) = generic_eval(gt, submission)
    else:
        logger.error(f"Submission file not found: {submission_path}")
        detection_score = -1.0
        loc_acc = loc_prec = loc_rec = loc_f1 = -1.0
        rca_acc = rca_prec = rca_rec = rca_f1 = -1.0

    trace_path = os.path.join(session.session_dir, MESSAGES_FILENAME)
    trace_metrics = AgentTraceParser(trace_path=trace_path).parse_trace()

    payload = {
        "detection_score": detection_score,
        "localization_accuracy": loc_acc,
        "localization_precision": loc_prec,
        "localization_recall": loc_rec,
        "localization_f1": loc_f1,
        "rca_accuracy": rca_acc,
        "rca_precision": rca_prec,
        "rca_recall": rca_rec,
        "rca_f1": rca_f1,
        "in_tokens": trace_metrics.get("in_tokens"),
        "out_tokens": trace_metrics.get("out_tokens"),
        "steps": trace_metrics.get("steps"),
        "tool_calls": trace_metrics.get("tool_calls"),
        "tool_errors": trace_metrics.get("tool_errors"),
    }
    out_path = Path(session.session_dir) / EVAL_METRICS_FILENAME
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    session.update_run_meta("eval_metrics", payload)
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
        ground_truth=textwrap.dedent(
            f"""\
                The root cause is {gt["root_cause_name"]}.
                The faulty devices are: {", ".join(gt["faulty_devices"])}.
            """
        ),
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
    run_judge: bool = False,
    judge_llm_provider: str | None = None,
    judge_model: str | None = None,
) -> None:
    """Close the session, then run metrics; LLM judge runs only when ``run_judge`` is set."""
    if run_judge and (not judge_llm_provider or not judge_model):
        raise ValueError(
            "--judge-provider and --judge-model are required when run_judge is enabled."
        )

    session = Session()
    session.load_running_session(session_id=session_id)
    resolved_session_id = session.session_id
    close_session(session_id=resolved_session_id, undeploy=destroy_env)
    run_eval_metrics(session_id=resolved_session_id)
    if run_judge:
        try:
            run_llm_judge(
                judge_llm_provider, judge_model, session_id=resolved_session_id
            )
        except Exception as exc:  # noqa: BLE001 - judge is post-hoc analysis
            # The session is closed and metrics are on disk; a transient judge
            # failure (LLM/network) must not fail the whole case. The judge can
            # be re-run offline on the closed session.
            print(
                f"WARNING: LLM judge failed for session {resolved_session_id}: {exc}\n"
                f"Re-run it later with: nika eval judge -p {judge_llm_provider} "
                f"-m {judge_model} --session_id {resolved_session_id}"
            )
