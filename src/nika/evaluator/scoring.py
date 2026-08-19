"""Rule-based scoring for detection and pair-based RCA submissions.

RCA scores the pair ``(resource.id, fault_type)`` as a set (precision, recall,
F1). Localization and fault-type identification are independent set metrics.
``*_accuracy`` keys are aliases of recall for leaderboard package schema 2
compatibility.
"""

from __future__ import annotations

from pydantic import ValidationError

from nika.evaluator.submissions import DetectionSubmission, RootCauseSubmission
from nika.problems.root_cause import RootCause


def score_detection(submission: dict, gt: dict) -> float:
    """Score binary anomaly detection."""
    try:
        is_anomaly = submission.get("is_anomaly", -1.0)
        if is_anomaly in ("True", "true", "1", 1, True, "yes", "Yes"):
            is_anomaly = True
        elif is_anomaly in ("False", "false", "0", 0, False, "no", "No"):
            is_anomaly = False
        else:
            return 0.0
        parsed = DetectionSubmission(is_anomaly=is_anomaly)
        return 1.0 if gt["is_anomaly"] == parsed.is_anomaly else 0.0
    except Exception:
        return -1.0


def _prf(pred: set, truth: set) -> tuple[float, float, float, float]:
    if not pred and not truth:
        return 1.0, 1.0, 1.0, 1.0
    tp = len(truth & pred)
    fp = len(pred - truth)
    fn = len(truth - pred)
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    accuracy_alias = recall
    return (
        round(float(accuracy_alias), 4),
        round(float(precision), 4),
        round(float(recall), 4),
        round(float(f1), 4),
    )


def _root_causes_from(payload: dict, *, field: str) -> list[RootCause] | None:
    raw = payload.get("root_causes")
    if raw is None:
        return None
    try:
        parsed = RootCauseSubmission.model_validate({"root_causes": raw})
    except ValidationError:
        return None
    return list(parsed.root_causes)


def score_rca_v2(submission: dict, gt: dict) -> dict[str, float]:
    """Joint (resource, fault_type) metrics plus localization and type submetrics."""
    invalid = {
        "rca_accuracy": -1.0,
        "rca_precision": -1.0,
        "rca_recall": -1.0,
        "rca_f1": -1.0,
        "localization_accuracy": -1.0,
        "localization_precision": -1.0,
        "localization_recall": -1.0,
        "localization_f1": -1.0,
        "fault_type_precision": -1.0,
        "fault_type_recall": -1.0,
        "fault_type_f1": -1.0,
    }
    pred_list = _root_causes_from(submission, field="submission")
    gt_list = _root_causes_from(gt, field="gt")
    if pred_list is None or gt_list is None:
        return invalid

    pred_pairs = {item.pair_key() for item in pred_list}
    gt_pairs = {item.pair_key() for item in gt_list}
    pred_res = {pair[0] for pair in pred_pairs}
    gt_res = {pair[0] for pair in gt_pairs}
    pred_types = {item.fault_type for item in pred_list}
    gt_types = {item.fault_type for item in gt_list}

    acc, prec, rec, f1 = _prf(pred_pairs, gt_pairs)
    loc_acc, loc_prec, loc_rec, loc_f1 = _prf(pred_res, gt_res)
    _t_acc, t_prec, t_rec, t_f1 = _prf(pred_types, gt_types)
    return {
        "rca_accuracy": acc,
        "rca_precision": prec,
        "rca_recall": rec,
        "rca_f1": f1,
        "localization_accuracy": loc_acc,
        "localization_precision": loc_prec,
        "localization_recall": loc_rec,
        "localization_f1": loc_f1,
        "fault_type_precision": t_prec,
        "fault_type_recall": t_rec,
        "fault_type_f1": t_f1,
    }
