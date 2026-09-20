"""Aggregation contract for ``nika eval summary``'s terminal report.

These cover the cases the raw summary CSV gets wrong: ``-1.0`` sentinels from
failed agents, healthy (no-fault) controls pooled with fault cases, and trials
that never landed being left out of the denominator. The last test pins the
report's headline to ``nika leaderboard pack``'s aggregate so the two published
numbers for one run cannot drift apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.workflows.benchmark.healthy import HEALTHY_PROBLEM
from nika.workflows.eval.report import (
    _HEALTHY_LABEL,
    GROUP_DIMENSIONS,
    build_summary_report,
)
from nika.workflows.leaderboard.aggregate import aggregate_trial_results
from nika.workflows.leaderboard.trial_results import trial_result_from_dir

pytestmark = pytest.mark.unit


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_trial(
    session_dir: Path,
    *,
    outcome: str = "success",
    problem: str | None = "link_down",
    scenario: str = "dc_clos",
    failure_domain: str = "link_interface",
    topo_size: str = "s",
    score: float = 1.0,
    predicted: list[str] | None = None,
    judge: bool = False,
) -> None:
    """Write one trial dir. ``problem=None`` makes it a healthy control case."""
    healthy = problem is None
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        session_dir / "run.json",
        {
            "status": "finished",
            "outcome": outcome,
            "session_id": session_dir.name,
            "scenario_name": scenario,
            "scenario_topo_size": topo_size,
            "problem_names": [HEALTHY_PROBLEM] if healthy else [problem],
            "agent_type": "byo.langgraph",
            "model": "test-model",
        },
    )
    _write_json(
        session_dir / "ground_truth.json",
        {
            "is_anomaly": not healthy,
            "root_causes": (
                [] if healthy else [{"resource_id": "node/pc1", "fault_type": problem}]
            ),
            "failure_domain": "" if healthy else failure_domain,
        },
    )
    # Failed agents get -1.0 sentinels; this is what pollutes a naive CSV mean.
    value = score if outcome == "success" else -1.0
    _write_json(
        session_dir / "eval_metrics.json",
        {
            "detection_score": value,
            "localization_f1": value,
            "rca_f1": value,
            "in_tokens": 10,
            "out_tokens": 5,
            "steps": 2,
            "tool_calls": 3,
            "tool_errors": 1,
        },
    )
    if outcome == "success":
        _write_json(
            session_dir / "submission.json",
            {
                "is_anomaly": not healthy,
                "root_causes": [
                    {"resource_id": "node/pc1", "fault_type": name}
                    for name in (predicted if predicted is not None else [problem])
                    if name
                ],
            },
        )
    if judge:
        _write_json(session_dir / "llm_judge.json", {"scores": {}})


def _report(root: Path, **kwargs):
    dirs = sorted(p for p in (root / "trials").iterdir() if p.is_dir())
    kwargs.setdefault("result_dir", root)
    return build_summary_report(dirs, **kwargs)


class TestEvalReportAggregation:
    def test_agent_failed_sentinels_score_zero_not_negative(self, tmp_path: Path):
        """A -1.0 sentinel must clamp to 0.0, never drag the mean below zero."""
        trials = tmp_path / "trials"
        _write_trial(trials / "ok__t01", outcome="success", score=1.0)
        _write_trial(trials / "bad__t01", outcome="agent_failed")

        report = _report(tmp_path, n_trials_expected=2)

        assert report.n_agent_failed == 1
        assert report.n_success == 1
        assert report.mean_rca_f1 == pytest.approx(0.5)
        assert report.mean_detection_score == pytest.approx(0.5)
        for breakdown in report.breakdowns:
            for row in breakdown.rows:
                assert row.stats.rca_f1 >= 0.0

    def test_missing_trials_count_as_zero_in_the_denominator(self, tmp_path: Path):
        """Two perfect trials out of four expected is 0.5, not 1.0."""
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01", score=1.0)
        _write_trial(trials / "b__t01", score=1.0)

        report = _report(tmp_path, n_trials_expected=4)

        assert report.n_trials_present == 2
        assert report.n_trials_expected == 4
        assert report.expected_is_known is True
        assert report.mean_rca_f1 == pytest.approx(0.5)

    def test_expected_count_read_from_run_config(self, tmp_path: Path):
        """``case_count x n_trials`` at the result root sets the denominator."""
        _write_json(tmp_path / "run.json", {"case_count": 2, "n_trials": 2})
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01", score=1.0)

        report = _report(tmp_path)

        assert report.n_trials_expected == 4
        assert report.expected_is_known is True
        assert report.mean_rca_f1 == pytest.approx(0.25)

    def test_planned_trial_count_overrides_case_times_n_trials(self, tmp_path: Path):
        """Scoped --task-id runs stamp planned_trial_count for the denominator."""
        _write_json(
            tmp_path / "run.json",
            {"case_count": 85, "n_trials": 3, "planned_trial_count": 1},
        )
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01", score=1.0)

        report = _report(tmp_path)

        assert report.n_trials_expected == 1
        assert report.expected_is_known is True
        assert report.mean_rca_f1 == pytest.approx(1.0)

    def test_unknown_expected_count_falls_back_to_present(self, tmp_path: Path):
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01", score=1.0)

        report = _report(tmp_path)

        assert report.expected_is_known is False
        assert report.n_trials_expected == 1
        assert report.mean_rca_f1 == pytest.approx(1.0)

    def test_healthy_controls_split_out_of_fault_scores(self, tmp_path: Path):
        """Healthy cases have no root cause, so they must not dilute RCA."""
        trials = tmp_path / "trials"
        _write_trial(trials / "fault__t01", problem="link_down", score=1.0)
        _write_trial(trials / "healthy__t01", problem=None, score=0.0)

        report = _report(tmp_path, n_trials_expected=2)

        assert report.fault_stats.n_trials == 1
        assert report.healthy_stats.n_trials == 1
        # Fault-only RCA stays 1.0 even though the pooled headline is 0.5.
        assert report.fault_stats.rca_f1 == pytest.approx(1.0)
        assert report.mean_rca_f1 == pytest.approx(0.5)
        # Breakdowns cover fault cases only.
        for breakdown in report.breakdowns:
            assert all(row.key != _HEALTHY_LABEL for row in breakdown.rows)
            assert sum(row.stats.n_trials for row in breakdown.rows) == 1

    def test_healthy_label_matches_benchmark_sentinel(self):
        """report.py mirrors the sentinel rather than importing the lab runtime."""
        assert _HEALTHY_LABEL == HEALTHY_PROBLEM

    def test_absent_judge_family_is_reported_not_scored(self, tmp_path: Path):
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01", judge=False)

        report = _report(tmp_path)

        assert "llm_judge_*" in report.empty_metric_families

        _write_trial(trials / "b__t01", judge=True)
        assert "llm_judge_*" not in _report(tmp_path).empty_metric_families

    def test_unreadable_trial_is_counted_not_scored(self, tmp_path: Path):
        """A session with no usable outcome cannot be scored either way."""
        trials = tmp_path / "trials"
        _write_trial(trials / "ok__t01", score=1.0)
        broken = trials / "broken__t01"
        _write_json(broken / "run.json", {"status": "finished"})

        report = _report(tmp_path)

        assert report.n_unreadable == 1
        assert report.n_trials_present == 1
        assert report.mean_rca_f1 == pytest.approx(1.0)

    def test_breakdowns_group_by_each_dimension(self, tmp_path: Path):
        trials = tmp_path / "trials"
        _write_trial(
            trials / "a__t01",
            problem="link_down",
            scenario="dc_clos",
            failure_domain="link_interface",
            topo_size="s",
            score=1.0,
        )
        _write_trial(
            trials / "b__t01",
            problem="bgp_acl_block",
            scenario="campus_lan",
            failure_domain="security",
            topo_size="l",
            score=0.0,
        )

        report = _report(tmp_path, dimensions=GROUP_DIMENSIONS)
        by_dimension = {b.dimension: b for b in report.breakdowns}

        assert set(by_dimension) == set(GROUP_DIMENSIONS)
        assert [r.key for r in by_dimension["domain"].rows] == [
            "link_interface",
            "security",
        ]
        assert [r.key for r in by_dimension["env"].rows] == ["dc_clos", "campus_lan"]
        assert [r.key for r in by_dimension["problem"].rows] == [
            "link_down",
            "bgp_acl_block",
        ]
        assert [r.key for r in by_dimension["size"].rows] == ["s", "l"]

    def test_rows_sort_by_the_requested_metric(self, tmp_path: Path):
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01", failure_domain="link_interface", score=0.25)
        _write_trial(trials / "b__t01", failure_domain="security", score=0.75)

        for metric in ("rca_f1", "localization_f1", "detection_score"):
            report = _report(tmp_path, metric=metric, dimensions=("domain",))
            rows = report.breakdowns[0].rows
            assert [r.key for r in rows] == ["security", "link_interface"]
            assert rows[0].stats.value(metric) == pytest.approx(0.75)

    def test_confusion_pairs_ground_truth_against_prediction(self, tmp_path: Path):
        trials = tmp_path / "trials"
        _write_trial(trials / "hit__t01", problem="link_down", predicted=["link_down"])
        _write_trial(
            trials / "miss__t01", problem="link_down", predicted=["bgp_acl_block"]
        )

        report = _report(tmp_path)
        pairs = {(row.gt, row.predicted): row.count for row in report.confusion}

        assert pairs[("link_down", "link_down")] == 1
        assert pairs[("link_down", "bgp_acl_block")] == 1

    def test_rejects_unknown_metric_and_dimension(self, tmp_path: Path):
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01")

        with pytest.raises(ValueError, match="Unknown report metric"):
            _report(tmp_path, metric="not_a_metric")
        with pytest.raises(ValueError, match="Unknown group dimension"):
            _report(tmp_path, dimensions=("not_a_dimension",))


class TestReportMatchesLeaderboard:
    def test_headline_equals_leaderboard_aggregate(self, tmp_path: Path):
        """One run must not yield two different official numbers."""
        trials = tmp_path / "trials"
        _write_trial(trials / "a__t01", score=1.0)
        _write_trial(trials / "b__t01", score=0.5)
        _write_trial(trials / "c__t01", outcome="agent_failed")
        _write_trial(trials / "d__t01", problem=None, score=1.0)
        expected = 6  # two trials never landed

        report = _report(tmp_path, n_trials_expected=expected)

        session_dirs = sorted(p for p in trials.iterdir() if p.is_dir())
        leaderboard = aggregate_trial_results(
            [
                trial_result_from_dir(
                    trial_id=d.name,
                    case_key=d.name,
                    trial_index=1,
                    scenario="dc_clos",
                    problem=d.name.split("__")[0],
                    session_dir=d,
                )
                for d in session_dirs
            ],
            n_trials_expected=expected,
        )

        assert report.mean_rca_f1 == pytest.approx(leaderboard.mean_rca_f1)
        assert report.mean_localization_f1 == pytest.approx(
            leaderboard.mean_localization_f1
        )
        assert report.mean_detection_score == pytest.approx(
            leaderboard.mean_detection_score
        )
        assert report.n_success == leaderboard.n_success
        assert report.n_agent_failed == leaderboard.n_agent_failed
        assert report.token_totals == leaderboard.token_totals
        assert report.steps_totals == leaderboard.steps_totals
