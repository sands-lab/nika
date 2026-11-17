"""The ``score`` reply carries release 0.1.0's scores in the API v1 envelope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.benchmark_engine.scores import SCORE_KEYS, grade_trial
from nika.workflows.eval.session import build_eval_metrics_payload, generic_eval

SCORING_ID = "rule-based-v2"
GT = {
    "schema_version": 2,
    "is_anomaly": True,
    "root_causes": [
        {
            "resource": {
                "kind": "interface",
                "node": "pc_0_1",
                "name": "eth0",
                "id": "interface/pc_0_1/eth0",
            },
            "resource_id": "interface/pc_0_1/eth0",
            "fault_type": "host_ip_conflict",
        }
    ],
    "root_cause_category": "end_host_failure",
    "faulty_devices": ["pc_0_1"],
    "root_cause_name": ["host_ip_conflict"],
}


def _submission(*causes: tuple[str, str], is_anomaly: bool = True) -> dict:
    return {
        "is_anomaly": is_anomaly,
        "root_causes": [{"resource_id": r, "fault_type": f} for r, f in causes],
        "faulty_devices": [],
        "root_cause_name": [],
    }


def _trial(tmp_path: Path, *, gt=GT, submission=None) -> Path:
    if gt is not None:
        (tmp_path / "ground_truth.json").write_text(json.dumps(gt), encoding="utf-8")
    if submission is not None:
        text = submission if isinstance(submission, str) else json.dumps(submission)
        (tmp_path / "submission.json").write_text(text, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "submission",
    [
        _submission(("interface/pc_0_1/eth0", "host_ip_conflict")),
        _submission(("node/pc_0_1", "host_ip_conflict")),
        _submission(("interface/pc_0_1/eth0", "host_incorrect_ip")),
        _submission(
            ("interface/pc_0_1/eth0", "host_ip_conflict"),
            ("node/leaf_router_0_0", "link_down"),
        ),
        _submission(is_anomaly=False),
    ],
)
def test_scored_values_are_the_release_scorers(tmp_path, submission) -> None:
    scores = grade_trial(_trial(tmp_path, submission=submission), scoring_id=SCORING_ID)
    expected = build_eval_metrics_payload(gt=GT, submission=submission, trace_metrics={})
    assert scores.status == "scored"
    assert scores.scoring_id == SCORING_ID
    assert scores.metrics == {key: expected[key] for key in SCORE_KEYS}
    assert scores.error is None


def test_no_submission_scores_zeros(tmp_path) -> None:
    scores = grade_trial(_trial(tmp_path), scoring_id=SCORING_ID)
    assert scores.status == "no_submission"
    assert scores.metrics == dict.fromkeys(SCORE_KEYS, 0.0)


def test_infra_evidence_without_submission_is_infra_error(tmp_path) -> None:
    scores = grade_trial(_trial(tmp_path), scoring_id=SCORING_ID, infra_evidence=True)
    assert scores.status == "infra_error"
    assert scores.metrics == dict.fromkeys(SCORE_KEYS)


def test_infra_evidence_does_not_override_a_submission(tmp_path) -> None:
    submission = _submission(("interface/pc_0_1/eth0", "host_ip_conflict"))
    scores = grade_trial(
        _trial(tmp_path, submission=submission),
        scoring_id=SCORING_ID,
        infra_evidence=True,
    )
    assert scores.status == "scored"
    assert scores.metrics["rca_f1"] == 1.0


def test_missing_ground_truth(tmp_path) -> None:
    assert grade_trial(_trial(tmp_path, gt=None), scoring_id=SCORING_ID).status == (
        "infra_error"
    )
    graded = grade_trial(
        _trial(tmp_path, gt=None, submission=_submission()), scoring_id=SCORING_ID
    )
    assert graded.status == "grading_error"
    assert graded.metrics == dict.fromkeys(SCORE_KEYS)


def test_unreadable_submission_is_a_grading_error(tmp_path) -> None:
    scores = grade_trial(_trial(tmp_path, submission="{not json"), scoring_id=SCORING_ID)
    assert scores.status == "grading_error"
    assert "submission.json" in (scores.error or "")


def test_unparsed_answer_scores_zero_not_minus_one(tmp_path) -> None:
    """0.1.0 wrote -1.0 for an answer it could not parse and counted it as 0."""
    submission = {"is_anomaly": True}
    assert generic_eval(GT, submission)["rca_f1"] == -1.0
    scores = grade_trial(_trial(tmp_path, submission=submission), scoring_id=SCORING_ID)
    assert scores.status == "scored"
    assert scores.metrics["rca_f1"] == 0.0
    assert scores.metrics["detection_score"] == 1.0


def test_grading_writes_nothing(tmp_path) -> None:
    trial = _trial(tmp_path, submission=_submission())
    before = sorted(p.name for p in trial.iterdir())
    grade_trial(trial, scoring_id=SCORING_ID)
    assert sorted(p.name for p in trial.iterdir()) == before
