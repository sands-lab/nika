"""Unit tests for score_status resolution and primary aggregation."""

from __future__ import annotations

import pytest

from nika.evaluator.scoring import GradingError, score_rca_v2
from nika.evaluator.score_status import (
    is_infra_error_evidence,
    resolve_score_status,
    scores_for_status,
)
from nika.workflows.leaderboard.aggregate import (
    aggregate_trial_results,
    score_for_primary,
)
from nika.workflows.leaderboard.schema import TrialResult

pytestmark = pytest.mark.unit


def _trial(
    *,
    trial_id: str,
    score_status: str,
    rca_f1: float | None,
    problem: str = "link_down",
    outcome: str | None = None,
) -> TrialResult:
    if outcome is None:
        outcome = (
            "success" if score_status in ("scored", "grading_error") else "agent_failed"
        )
    metrics = {
        "rca_f1": rca_f1,
        "localization_f1": rca_f1,
        "detection_score": rca_f1,
    }
    return TrialResult(
        trial_id=trial_id,
        case_key=trial_id.rsplit("__", 1)[0],
        trial_index=1,
        scenario="dc_clos",
        problem=problem,
        outcome=outcome,  # type: ignore[arg-type]
        score_status=score_status,  # type: ignore[arg-type]
        metrics=metrics,
        gt_fault_types=[problem] if problem != "healthy" else [],
        predicted_fault_types=[problem] if score_status == "scored" else None,
    )


class TestScoreStatusResolution:
    def test_submission_first_scored(self) -> None:
        assert (
            resolve_score_status(has_submission=True, grading_failed=False) == "scored"
        )

    def test_submission_first_grading_error(self) -> None:
        assert (
            resolve_score_status(has_submission=True, grading_failed=True)
            == "grading_error"
        )

    def test_no_submission_default(self) -> None:
        assert resolve_score_status(has_submission=False) == "no_submission"

    def test_no_submission_infra(self) -> None:
        assert (
            resolve_score_status(has_submission=False, infra_evidence=True)
            == "infra_error"
        )

    def test_missing_ground_truth_is_infra(self) -> None:
        assert (
            resolve_score_status(has_submission=False, has_ground_truth=False)
            == "infra_error"
        )
        assert (
            resolve_score_status(has_submission=True, has_ground_truth=False)
            == "scored"
        )

    def test_infra_patterns(self) -> None:
        assert is_infra_error_evidence("OpenAI APIConnectionError: connection reset")
        assert is_infra_error_evidence("MCP transport failed")
        assert not is_infra_error_evidence("agent exceeded max_steps")
        assert not is_infra_error_evidence("wrong diagnosis")

    def test_infra_patterns_ignore_case_ids_in_paths(self) -> None:
        assert not is_infra_error_evidence(
            "Agent completed without writing required submission: "
            "/results/run/trials/campus_lan__dns_record_error__h139fe82d0c0f__t01/"
            "submission.json"
        )
        assert is_infra_error_evidence("connection refused at http://localhost:8000/v1")


class TestPrimaryAggregation:
    def test_healthy_no_submission_is_zero_not_one(self) -> None:
        trials = [
            _trial(
                trial_id="healthy__t01",
                score_status="no_submission",
                rca_f1=0.0,
                problem="healthy",
            )
        ]
        metrics = aggregate_trial_results(trials, n_trials_expected=1)
        assert metrics.mean_rca_f1 == pytest.approx(0.0)
        assert metrics.mean_detection_score == pytest.approx(0.0)
        assert metrics.submission_rate == pytest.approx(0.0)

    def test_one_scored_two_timeouts_is_one_third(self) -> None:
        trials = [
            _trial(trial_id="a__t01", score_status="scored", rca_f1=1.0),
            _trial(trial_id="a__t02", score_status="no_submission", rca_f1=0.0),
            _trial(trial_id="a__t03", score_status="no_submission", rca_f1=0.0),
        ]
        metrics = aggregate_trial_results(trials, n_trials_expected=3)
        assert metrics.mean_rca_f1 == pytest.approx(1.0 / 3.0)
        assert metrics.submission_rate == pytest.approx(1.0 / 3.0)
        assert metrics.conditional_mean_rca_f1 == pytest.approx(1.0)
        assert metrics.status_counts["scored"] == 1
        assert metrics.status_counts["no_submission"] == 2

    def test_negatives_never_enter_mean(self) -> None:
        assert score_for_primary(-1.0, score_status="scored") == 0.0
        assert score_for_primary(None, score_status="infra_error") == 0.0
        assert score_for_primary(0.5, score_status="scored") == 0.5

    def test_invalid_pred_root_causes_score_zero_not_sentinel(self) -> None:
        gt = {
            "is_anomaly": True,
            "root_causes": [{"resource_id": "node/pc1", "fault_type": "link_down"}],
        }
        scores = score_rca_v2({"root_causes": "not-a-list"}, gt)
        assert scores["rca_f1"] == 0.0
        assert all(v >= 0.0 for v in scores.values())

    def test_invalid_gt_raises_grading_error(self) -> None:
        with pytest.raises(GradingError):
            score_rca_v2({"root_causes": []}, {"root_causes": "bad"})


class TestValidateRejectsBadStatuses:
    def test_grading_error_and_infra_rejected(self) -> None:
        trials = [
            _trial(trial_id="a__t01", score_status="scored", rca_f1=1.0),
            _trial(trial_id="b__t01", score_status="grading_error", rca_f1=None),
        ]
        metrics = aggregate_trial_results(trials, n_trials_expected=2)
        assert metrics.status_counts["grading_error"] == 1

        trials2 = [
            _trial(trial_id="a__t01", score_status="scored", rca_f1=1.0),
            _trial(trial_id="b__t01", score_status="infra_error", rca_f1=None),
        ]
        metrics2 = aggregate_trial_results(trials2, n_trials_expected=2)
        assert metrics2.status_counts["infra_error"] == 1
        assert int(metrics.status_counts.get("grading_error") or 0) > 0
        assert int(metrics2.status_counts.get("infra_error") or 0) > 0


class TestScoresForStatus:
    def test_placeholders(self) -> None:
        zeros = scores_for_status("no_submission")
        assert zeros["rca_f1"] == 0.0
        nulls = scores_for_status("infra_error")
        assert nulls["rca_f1"] is None
        with pytest.raises(ValueError):
            scores_for_status("scored")
