"""Rule-based scoring for detection and pair-based RCA submissions.

RCA scores the pair ``(resource.id, fault_type)`` as a set (precision, recall,
F1). Localization and fault-type identification are independent set metrics.
A predicted interface inside a ground-truth node or link localizes it.
``*_accuracy`` keys are aliases of recall for backward compatibility.

Invalid agent payloads score as ``0.0`` under ``scored``. GT/scorer failures
are signaled by raising so callers can stamp ``grading_error`` with nulls.
"""

from __future__ import annotations

from pydantic import ValidationError

from nika.evaluator.submissions import DetectionSubmission, RootCauseSubmission
from nika.problems.rca import RootCause
from nika.problems.rca.models import FaultResource, ResourceKind
from nika.evaluator.score_status import zero_scores


class GradingError(Exception):
    """Ground truth or scorer failed; trial should be ``grading_error``."""


def _parse_is_anomaly(value: object) -> bool | None:
    if value in ("True", "true", "1", 1, True, "yes", "Yes"):
        return True
    if value in ("False", "false", "0", 0, False, "no", "No"):
        return False
    return None


def score_detection(submission: dict, gt: dict) -> float:
    """Score binary anomaly detection. Unparseable ``is_anomaly`` → 0.0."""
    try:
        is_anomaly = _parse_is_anomaly(submission.get("is_anomaly"))
        if is_anomaly is None:
            return 0.0
        parsed = DetectionSubmission(is_anomaly=is_anomaly)
        return 1.0 if gt["is_anomaly"] == parsed.is_anomaly else 0.0
    except Exception as exc:  # noqa: BLE001 - surface as grading failure
        raise GradingError(f"detection scoring failed: {exc}") from exc


def _prf(
    n_pred: int, n_truth: int, tp: int, *, empty_credit: bool
) -> tuple[float, float, float, float]:
    if not n_pred and not n_truth:
        score = 1.0 if empty_credit else 0.0
        return score, score, score, score
    fp = n_pred - tp
    fn = n_truth - tp
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


def resource_within(pred: FaultResource, truth: FaultResource) -> bool:
    """Whether *pred* names *truth* or a more specific part of it.

    An interface on a ground-truth node, or an endpoint interface of a
    ground-truth link, localizes that node or link.
    """
    if pred.id == truth.id:
        return True
    if pred.kind != ResourceKind.INTERFACE:
        return False
    if truth.kind == ResourceKind.NODE:
        return pred.node == truth.node
    if truth.kind == ResourceKind.LINK:
        return f"{pred.node}:{pred.name}" in str(truth.name).split("--")
    return False


def _matched(preds: list, truths: list, matches) -> int:
    """Size of a maximum one-to-one matching between *preds* and *truths*.

    Each ground-truth item is matched at most once, so extra predictions that
    fall inside the same resource stay false positives.
    """
    owner: dict[int, int] = {}

    def assign(p: int, seen: set[int]) -> bool:
        for t, truth in enumerate(truths):
            if t in seen or not matches(preds[p], truth):
                continue
            seen.add(t)
            if t not in owner or assign(owner[t], seen):
                owner[t] = p
                return True
        return False

    return sum(assign(p, set()) for p in range(len(preds)))


def parse_root_causes(payload: dict) -> list[RootCause] | None:
    """``payload['root_causes']`` as ``RootCause`` items, or ``None`` if missing/invalid."""
    raw = payload.get("root_causes")
    if raw is None:
        return None
    try:
        parsed = RootCauseSubmission.model_validate({"root_causes": raw})
    except ValidationError:
        return None
    return list(parsed.root_causes)


def score_rca_v2(submission: dict, gt: dict) -> dict[str, float]:
    """Joint (resource, fault_type) metrics plus localization and type submetrics.

    A predicted resource matches a ground-truth resource when it is the same
    resource or a more specific part of it (:func:`resource_within`), one to
    one. Fault types match exactly. Empty predicted and ground-truth sets
    (a healthy case) score ``1.0`` only when the submission also says
    ``is_anomaly: false``.

    Invalid agent ``root_causes`` → all ``0.0`` (still ``scored``).
    Unparseable GT → ``GradingError``.
    """
    zeros = {k: 0.0 for k in zero_scores()}
    pred_list = parse_root_causes(submission)
    gt_list = parse_root_causes(gt)
    if gt_list is None:
        raise GradingError("ground truth root_causes missing or invalid")
    if pred_list is None:
        return {
            "rca_accuracy": zeros["rca_accuracy"],
            "rca_precision": zeros["rca_precision"],
            "rca_recall": zeros["rca_recall"],
            "rca_f1": zeros["rca_f1"],
            "localization_accuracy": zeros["localization_accuracy"],
            "localization_precision": zeros["localization_precision"],
            "localization_recall": zeros["localization_recall"],
            "localization_f1": zeros["localization_f1"],
            "fault_type_precision": zeros["fault_type_precision"],
            "fault_type_recall": zeros["fault_type_recall"],
            "fault_type_f1": zeros["fault_type_f1"],
        }

    healthy_verdict = _parse_is_anomaly(submission.get("is_anomaly")) is False
    pred_pairs = list({item.pair_key(): item for item in pred_list}.values())
    gt_pairs = list({item.pair_key(): item for item in gt_list}.values())
    pred_res = list({item.resource.id: item.resource for item in pred_list}.values())
    gt_res = list({item.resource.id: item.resource for item in gt_list}.values())
    pred_types = {item.fault_type for item in pred_list}
    gt_types = {item.fault_type for item in gt_list}

    pair_tp = _matched(
        pred_pairs,
        gt_pairs,
        lambda p, t: (
            p.fault_type == t.fault_type and resource_within(p.resource, t.resource)
        ),
    )
    res_tp = _matched(pred_res, gt_res, resource_within)
    acc, prec, rec, f1 = _prf(
        len(pred_pairs), len(gt_pairs), pair_tp, empty_credit=healthy_verdict
    )
    loc_acc, loc_prec, loc_rec, loc_f1 = _prf(
        len(pred_res), len(gt_res), res_tp, empty_credit=healthy_verdict
    )
    _t_acc, t_prec, t_rec, t_f1 = _prf(
        len(pred_types),
        len(gt_types),
        len(pred_types & gt_types),
        empty_credit=healthy_verdict,
    )
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
