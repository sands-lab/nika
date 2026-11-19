"""Main task weighting and paired release slice statistics."""

import pytest

from nika.workflows.benchmark.network_identity import (
    dataset_row,
    DATASET_METADATA_FIELDS,
)
from nika.workflows.benchmark.trials import task_id_for_row
from nika.workflows.leaderboard.aggregate import (
    aggregate_trial_results,
    metrics_nearly_equal,
)
from nika.workflows.leaderboard.schema import TrialResult

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("field", ["mean_rca_f1", "case_consistency"])
def test_comparison_rejects_nonfinite_scores(field):
    expected = aggregate_trial_results([], n_trials_expected=0)
    actual = expected.model_copy(update={field: float("nan")})
    assert any(field in issue for issue in metrics_nearly_equal(expected, actual))


def _trial(row, score, index=1):
    key = task_id_for_row(row)
    return TrialResult(
        trial_id=f"{key}__t{index:02}",
        case_key=key,
        trial_index=index,
        scenario=row["scenario"],
        problem=row["problem"],
        outcome="success",
        score_status="scored",
        metrics={"rca_f1": score, "localization_f1": score, "detection_score": score},
        dataset={k: row[k] for k in DATASET_METADATA_FIELDS if k in row},
    )


def _row(fault="link_down", *, size="s", slice_name="core", profile="frr"):
    row = dataset_row(
        {
            "scenario": "dc_clos",
            "topo_size": size,
            "device_profile": profile,
            "problem": fault,
            "inject": {},
        },
        slice_name=slice_name,
    )

    if slice_name == "vendor_twin":
        row["vendor_tier"] = "C"
    return row


def test_main_is_task_mean_with_optional_rows_excluded():
    core, healthy, multi = (
        _row(),
        _row("healthy", slice_name="healthy"),
        _row("link_down+link_flap", slice_name="multi_fault"),
    )
    twin = _row(slice_name="vendor_twin", profile="sonic")
    twin["base_task_id"] = task_id_for_row(core)
    third = _row(size="l", slice_name="third_size")
    trials = [
        _trial(core, 0, 1),
        _trial(core, 1, 2),
        _trial(core, 1, 3),
        _trial(healthy, 1),
        _trial(multi, 0),
        _trial(twin, 1),
        _trial(third, 1),
    ]
    result = aggregate_trial_results(trials, n_trials_expected=len(trials))
    assert result.main_score == pytest.approx(((2 / 3) + 1 + 0) / 3)
    assert result.mean_rca_f1 == result.main_score
    assert result.multi_fault_mean_rca_f1 == 0
    assert result.healthy_false_positive_rate == 0
    assert result.vendor_deltas["C"]["sonic"]["delta"] == pytest.approx(1 / 3)
    assert (
        result.vendor_deltas["C"]["sonic"]["ci95"] is None
    )  # One fault cannot give a population CI.
    baseline = aggregate_trial_results(trials[:-2], n_trials_expected=5)
    assert baseline.main_score == result.main_score
    assert set(result.size_slices) == {"generator_families"}


def test_expected_release_tasks_and_trials_receive_zero_when_missing():
    core, healthy = _row(), _row("healthy", slice_name="healthy")
    result = aggregate_trial_results(
        [_trial(core, 1)],
        n_trials_expected=6,
        task_rows=[core, healthy],
        trials_per_task=3,
    )
    assert result.main_score == pytest.approx(1 / 6)
    assert result.healthy_false_positive_rate is None


def test_missing_rca_score_counts_as_zero():
    core = _row()
    trials = [_trial(core, 1, 1), _trial(core, None, 2)]
    result = aggregate_trial_results(
        trials, n_trials_expected=2, task_rows=[core], trials_per_task=2
    )
    assert result.main_score == pytest.approx(0.5)
    assert result.mean_rca_f1 == pytest.approx(0.5)
    assert result.conditional_mean_rca_f1 == pytest.approx(0.5)
    for trial in trials:
        trial.dataset = {}
    assert aggregate_trial_results(trials, n_trials_expected=2).mean_rca_f1 == 0.5


def test_missing_requested_objective_counts_as_zero():
    core = _row()
    trials = [_trial(core, 1, 1), _trial(core, None, 2)]
    result = aggregate_trial_results(
        trials, n_trials_expected=2, task_rows=[core], trials_per_task=2
    )
    assert result.mean_localization_f1 == pytest.approx(0.5)
    assert result.mean_detection_score == pytest.approx(0.5)
    assert result.conditional_mean_localization_f1 == pytest.approx(0.5)
    assert result.conditional_mean_detection_score == pytest.approx(0.5)

    core["objectives"] = ["detection", "rca"]
    trials = [_trial(core, 1, 1), _trial(core, None, 2)]
    result = aggregate_trial_results(
        trials, n_trials_expected=2, task_rows=[core], trials_per_task=2
    )
    assert result.mean_detection_score == pytest.approx(0.5)
    assert result.mean_localization_f1 == 0
    assert result.conditional_mean_localization_f1 is None


@pytest.mark.parametrize("tier", ["A", "C"])
def test_vendor_ci_clusters_by_fault_and_averages_trials_first(tier):
    rows, trials = [], []
    vendors = (
        ("iosxr", "routeros", "sonic", "nokia_srlinux") if tier == "A" else ("sonic",)
    )
    for fault, base_score, vendor_score in (
        ("bgp_acl_block", 1, 0),
        ("bgp_asn_misconfig", 0, 1),
    ):
        base = dataset_row(
            {"scenario": "isp_pdh", "problem": fault, "inject": {"host_name": "n1"}}
        )
        rows.append(base)
        trials.extend(_trial(base, base_score, i) for i in (1, 2, 3))
        for vendor in vendors:
            twin = dataset_row(
                {**base, "device_profile": vendor}, slice_name="vendor_twin"
            )
            twin["base_task_id"] = task_id_for_row(base)
            twin["vendor_tier"] = tier
            rows.append(twin)
            trials.extend(_trial(twin, vendor_score, i) for i in (1, 2, 3))
    result = aggregate_trial_results(
        trials, n_trials_expected=len(trials), task_rows=rows, trials_per_task=3
    )
    delta = result.vendor_deltas[tier]["sonic"]
    assert delta["delta"] == 0
    assert delta["n_pairs"] == delta["n_fault_clusters"] == 2
    assert delta["ci95"][0] < 0 < delta["ci95"][1]
    assert set(result.vendor_deltas) == {tier}
    if tier == "A":
        pooled = result.vendor_deltas["A"]["all_vendors"]
        assert pooled["delta"] == delta["delta"]
        assert pooled["n_pairs"] == 8
        assert pooled["n_fault_clusters"] == 2
        # Reusing four vendor deltas does not multiply independent information.
        assert pooled["ci95"] == pytest.approx(delta["ci95"])


def test_isp_size_slices_are_separate_and_exceptions_remain_in_main():
    excluded = dataset_row(
        {
            "scenario": "isp_abilene",
            "device_profile": "routeros",
            "problem": "bgp_address_family_not_activated",
            "inject": {},
        }
    )
    excluded["size_comparison_excluded"] = True
    isp = dataset_row({"scenario": "isp_geant", "problem": "link_down", "inject": {}})
    generated = _row()
    result = aggregate_trial_results(
        [_trial(excluded, 0), _trial(isp, 1), _trial(generated, 1)], n_trials_expected=3
    )
    assert result.main_score == pytest.approx(2 / 3)
    assert set(result.size_slices) == {"generator_families", "isp_graph_classes"}
    assert (
        sum(
            s["n_tasks"]
            for fam in result.size_slices["isp_graph_classes"].values()
            for s in fam.values()
        )
        == 1
    )
