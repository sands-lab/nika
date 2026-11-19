import pytest

from nika.service.pingmesh import baseline
from nika.service.pingmesh.baseline import (
    healthy_latency_baselines,
    sustained_high_latency,
)

pytestmark = pytest.mark.unit


def test_healthy_baseline_uses_distinct_samples_from_each_path_after_readiness():
    rows = [
        {
            "metric": {"source": "a", "target": "b"},
            "values": [[1, 9], [2, 0.1], [3, 0.2], [4, 0.8], [4, 0.8]],
        },
        {
            "metric": {"source": "b", "target": "a"},
            "values": [[2, 0.5], [3, 0.6], [4, 0.7]],
        },
        {
            "metric": {"source": "a", "target": "c"},
            "values": [[2, 0.1], [3, 0], [4, "NaN"]],
        },
    ]
    assert healthy_latency_baselines(rows, after=2) == {
        ("a", "b"): 0.2,
        ("b", "a"): 0.6,
    }


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ([0.01, 0.2, 0.01], 0),
        ([0.2], 0),
        ([0.2, 0.2], 2),
        ([0.2, 0.2, 0.01], 2),
        ([0.2, 0.2, 0.01, 0.01], 0),
    ],
)
def test_sustained_high_latency_requires_repeated_majority(samples, expected):
    assert sustained_high_latency(samples, 0.1) == expected


def test_calibration_persists_only_post_readiness_observations(tmp_path, monkeypatch):
    def query(_api, *, query, limit):
        if query.startswith("pingmesh_endpoint_supported"):
            return [{"node": "a"}, {"node": "b"}]
        if query == "pingmesh_coverage_window_seconds":
            return [{"value": 30}]
        if query == "pingmesh_candidate_targets":
            return [{"value": 4}]
        return [
            {
                "metric": {"source": a, "target": b},
                "values": [[99, 9], [101, 0.1], [102, 0.2], [103, 0.3]],
            }
            for a, b in [("a", "b"), ("b", "a")]
        ]

    monkeypatch.setattr(baseline, "prometheus_query_for", query)
    monkeypatch.setattr(baseline.time, "time", lambda: 100)
    monkeypatch.setattr(baseline.time, "monotonic", lambda: 0)
    monkeypatch.setattr(baseline.time, "sleep", lambda _: None)
    captured = baseline.capture_latency_baseline(None, str(tmp_path), None)
    assert captured == {("a", "b"): 0.2, ("b", "a"): 0.2}
    assert baseline.read_latency_baseline(str(tmp_path)) == captured
    restored_dir = tmp_path.parent / (tmp_path.name + "-rebound")
    tmp_path.rename(restored_dir)
    assert baseline.read_latency_baseline(str(restored_dir)) == captured


def test_calibration_deadline_persists_unavailable_paths(tmp_path, monkeypatch):
    warnings = []
    monkeypatch.setattr(
        baseline.system_logger, "warning", lambda *args: warnings.append(args)
    )

    def query(_api, *, query, limit):
        if query.startswith("pingmesh_endpoint_supported"):
            return [{"node": "a"}, {"node": "b"}]
        if query == "pingmesh_coverage_window_seconds":
            return [{"value": 30}]
        if query == "pingmesh_candidate_targets":
            return [{"value": 4}]
        return [
            {
                "metric": {"source": "a", "target": "b"},
                "values": [[101, 0.1], [102, 0.2], [103, 0.3]],
            }
        ]

    now = [0]
    monkeypatch.setattr(baseline, "prometheus_query_for", query)
    monkeypatch.setattr(baseline.time, "time", lambda: 100 + now[0])
    monkeypatch.setattr(baseline.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        baseline.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    captured = baseline.capture_latency_baseline(None, str(tmp_path), None)
    assert now[0] == baseline.CALIBRATION_TIMEOUT_SECONDS
    assert captured == {("a", "b"): 0.2, ("b", "a"): None}
    assert baseline.read_latency_baseline(str(tmp_path)) == captured
    assert '"rtt_seconds": null' in (tmp_path / baseline.BASELINE_FILE).read_text()
    assert "baseline unavailable for 1 of 2 paths" in (warnings[0][0] % warnings[0][1:])
    restored_dir = tmp_path.parent / (tmp_path.name + "-rebound")
    tmp_path.rename(restored_dir)
    assert baseline.read_latency_baseline(str(restored_dir)) == captured


def test_calibration_queries_every_path_in_a_large_inventory(tmp_path, monkeypatch):
    limits = []

    def query(_api, *, query, limit):
        if query.startswith("pingmesh_endpoint_supported"):
            return [{"node": str(i)} for i in range(130)]
        if query in ("pingmesh_coverage_window_seconds", "pingmesh_candidate_targets"):
            return [{"value": 30}]
        limits.append(limit)
        return []

    clock = iter([0, 0, 601, 601, 601])
    monkeypatch.setattr(baseline, "prometheus_query_for", query)
    monkeypatch.setattr(baseline.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(baseline.time, "sleep", lambda _: None)
    baseline.capture_latency_baseline(None, str(tmp_path), None)
    assert limits == [None]
