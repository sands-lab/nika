"""Task diagnosis objectives: identity, scoring, and aggregation."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.objectives import (
    normalize_objectives,
    objectives_field,
    objectives_prompt,
)
from nika.workflows.benchmark.resume import benchmark_row_identity, benchmark_option_id
from nika.workflows.eval.session import generic_eval
from nika.workflows.leaderboard.aggregate import aggregate_trial_results
from nika.workflows.leaderboard.schema import TrialResult

pytestmark = pytest.mark.contract

_ROW = {
    "scenario": "dc_clos",
    "problem": "link_down",
    "topo_size": "s",
    "inject": {"host_name": "leaf_1", "intf_name": "eth1"},
}
_GT = {
    "is_anomaly": True,
    "root_causes": [
        {"resource": {"kind": "node", "node": "leaf_1"}, "fault_type": "link_down"}
    ],
}


def test_objectives_always_include_rca_and_default_to_all() -> None:
    assert normalize_objectives(None) == ("detection", "localization", "rca")
    assert normalize_objectives(["rca", "detection"]) == ("detection", "rca")
    with pytest.raises(ValueError, match="must include 'rca'"):
        normalize_objectives(["detection", "localization"])
    with pytest.raises(ValueError, match="Unknown objectives"):
        normalize_objectives(["rca", "repair"])


def test_default_objectives_keep_the_task_identity() -> None:
    every = ["detection", "localization", "rca"]
    assert objectives_field(every) == {}
    assert objectives_prompt(every) == ""
    assert benchmark_row_identity({**_ROW, "objectives": every}) == (
        benchmark_row_identity(_ROW)
    )
    assert benchmark_row_identity({**_ROW, "objectives": ["rca"]}) != (
        benchmark_row_identity(_ROW)
    )
    assert "root cause" in objectives_prompt(["rca"])
    assert benchmark_option_id({**_ROW, "objectives": every}) == benchmark_option_id(
        _ROW
    )
    assert benchmark_option_id({**_ROW, "objectives": ["rca"]}) != benchmark_option_id(
        _ROW
    )


def test_unasked_objectives_score_null_and_leave_the_means() -> None:
    submission = {"is_anomaly": False, "root_causes": []}
    full = generic_eval(_GT, submission)
    rca_only = generic_eval({**_GT, "objectives": ["rca"]}, submission)
    assert full["detection_score"] == 0.0
    assert rca_only["detection_score"] is None
    assert rca_only["localization_f1"] is None
    assert rca_only["rca_f1"] == full["rca_f1"]

    def trial(index: int, metrics: dict) -> TrialResult:
        return TrialResult(
            trial_id=f"t{index}",
            case_key=f"c{index}",
            trial_index=1,
            scenario="dc_clos",
            problem="link_down",
            outcome="success",
            score_status="scored",
            metrics=metrics,
        )

    asked = {"detection_score": 1.0, "localization_f1": 1.0, "rca_f1": 1.0}
    unasked = {"detection_score": None, "localization_f1": None, "rca_f1": 0.0}
    result = aggregate_trial_results(
        [trial(1, asked), trial(2, unasked)], n_trials_expected=2
    )
    assert result.mean_detection_score == 1.0
    assert result.mean_localization_f1 == 1.0
    assert result.mean_rca_f1 == 0.5
