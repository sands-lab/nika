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


def test_sampling_returns_available_paths_without_waiting(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(baseline.time, "time", lambda: now[0])
    monkeypatch.setattr(
        baseline.time, "sleep", lambda _: pytest.fail("Sampling must not wait")
    )
    baseline.initialize_latency_baseline(str(tmp_path))
    now[0] = 104

    def query(_api, *, query, limit):
        assert limit is None
        if query.startswith("pingmesh_endpoint_supported"):
            return [{"node": "a"}, {"node": "b"}]
        return [
            {
                "metric": {"source": "a", "target": "b"},
                "values": [[99, 9], [101, 0.1], [102, 0.2], [103, 0.3], [105, 8]],
            }
        ]

    monkeypatch.setattr(baseline, "prometheus_query_for", query)
    captured = baseline.capture_latency_baseline(None, str(tmp_path), None)
    assert captured == {("a", "b"): 0.2, ("b", "a"): None}
    assert baseline.read_latency_baseline(str(tmp_path)) == captured


def test_frozen_baseline_never_learns_fault_samples_and_survives_restore(
    tmp_path, monkeypatch
):
    import json

    now = [100.0]
    monkeypatch.setattr(baseline.time, "time", lambda: now[0])
    baseline.initialize_latency_baseline(str(tmp_path))
    now[0] = 104

    def query(_api, *, query, limit):
        if query.startswith("pingmesh_endpoint_supported"):
            return [{"node": "a"}, {"node": "b"}]
        return [
            {
                "metric": {"source": "a", "target": "b"},
                "values": [[101, 0.1], [102, 0.2], [103, 0.3]],
            }
        ]

    monkeypatch.setattr(baseline, "prometheus_query_for", query)
    captured = baseline.capture_latency_baseline(None, str(tmp_path), None, freeze=True)
    assert captured == {("a", "b"): 0.2, ("b", "a"): None}
    assert json.loads((tmp_path / baseline.WINDOW_FILE).read_text())["frozen_at"] == 104
    saved = (tmp_path / baseline.BASELINE_FILE).read_bytes()
    now[0] = 1000
    monkeypatch.setattr(
        baseline,
        "prometheus_query_for",
        lambda *a, **kw: pytest.fail("Frozen baseline queried"),
    )
    restored = tmp_path.parent / (tmp_path.name + "-restored")
    tmp_path.rename(restored)
    assert baseline.capture_latency_baseline(None, str(restored), None) == captured
    assert (restored / baseline.BASELINE_FILE).read_bytes() == saved


def test_existing_healthy_path_values_do_not_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline.time, "time", lambda: 100)
    baseline.initialize_latency_baseline(str(tmp_path))
    baseline.write_json_atomic(
        tmp_path / baseline.BASELINE_FILE,
        [{"source": "a", "target": "b", "rtt_seconds": 0.01}],
    )
    monkeypatch.setattr(baseline.time, "time", lambda: 104)
    monkeypatch.setattr(
        baseline,
        "prometheus_query_for",
        lambda _api, *, query, limit: (
            [{"node": "a"}, {"node": "b"}]
            if query.startswith("pingmesh_endpoint_supported")
            else [
                {
                    "metric": {"source": "a", "target": "b"},
                    "values": [[101, 1], [102, 1], [103, 1]],
                }
            ]
        ),
    )
    assert (
        baseline.capture_latency_baseline(None, str(tmp_path), None)[("a", "b")] == 0.01
    )


def test_sampling_keeps_every_path_in_a_large_inventory(tmp_path, monkeypatch):
    limits = []
    monkeypatch.setattr(baseline.time, "time", lambda: 100)
    baseline.initialize_latency_baseline(str(tmp_path))
    monkeypatch.setattr(
        baseline.time, "sleep", lambda _: pytest.fail("Sampling must not wait")
    )

    def query(_api, *, query, limit):
        limits.append(limit)
        return (
            [{"node": str(i)} for i in range(130)]
            if query.startswith("pingmesh_endpoint_supported")
            else []
        )

    monkeypatch.setattr(baseline, "prometheus_query_for", query)
    captured = baseline.capture_latency_baseline(None, str(tmp_path), None)
    assert len(captured) == 130 * 129
    assert all(value is None for value in captured.values())
    assert limits == [None, None]
