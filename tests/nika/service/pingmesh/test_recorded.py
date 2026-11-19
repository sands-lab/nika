"""Healthy auditing uses recorded outcomes and path-specific latency baselines."""

import pytest

from nika.net_env.probe_intent import ProbeIntent
from nika.service.pingmesh import recorded

pytestmark = pytest.mark.unit


def test_recorded_audit_reports_policy_violations_and_failed_scrapes(monkeypatch):
    queries = []

    def query(_api, *, query, limit):
        assert limit is None
        queries.append(query)
        if "probe_success" in query:
            return [
                {
                    "metric": {
                        "source": "a",
                        "target": "b",
                        "expected": "isolated",
                        "module": "icmp",
                    },
                    "value": 1,
                }
            ]
        if query.startswith("up"):
            return [{"metric": {"source": "b", "target": "a"}, "value": 0}]
        return []

    monkeypatch.setattr(recorded, "prometheus_query_for", query)
    result = recorded.recorded_mesh(
        None, intent=ProbeIntent(max_expected_rtt_ms=2000), baselines={}
    )
    assert {row["type"] for row in result["anomalies"]} == {
        "policy_violation",
        "unknown",
    }
    assert result["summary"]["total_pairs"] == 1
    assert any("max_over_time" in q and "isolated" in q for q in queries)
    assert (
        queries[-1]
        == 'probe_icmp_duration_seconds{phase="rtt",expected="reachable"}[1m] offset 1m'
    )


@pytest.mark.parametrize(
    ("samples", "anomaly"),
    [
        ([0.01, 0.104, 0.01, 0.01], False),
        ([0.104], False),
        ([0.104, 0.104, 0.01], True),
        ([0.104, 0.104], True),
        ([0.104, 0.104, 0.01, 0.01], False),
    ],
)
def test_recorded_latency_requires_repeated_majority_of_raw_samples(
    monkeypatch, samples, anomaly
):
    labels = {"source": "a", "target": "b", "expected": "reachable"}

    def query(_api, *, query, limit):
        if "probe_success" in query:
            return [{"metric": labels, "value": 1}]
        if "duration_seconds" in query:
            return [{"metric": labels, "values": list(enumerate(samples))}]
        return []

    monkeypatch.setattr(recorded, "prometheus_query_for", query)
    # Re-reading overlapping windows must not promote a single outlier.
    for _ in range(2):
        result = recorded.recorded_mesh(None, baselines={("a", "b"): 0.01})
        assert bool(result["anomalies"]) is anomaly
        if anomaly:
            assert result["anomalies"][0]["type"] == "high_latency"


@pytest.mark.parametrize("earlier", [[], [0.2], [0.2, 0.01, 0.01]])
def test_recorded_latency_requires_a_majority_in_both_windows(monkeypatch, earlier):
    labels = {"source": "a", "target": "b", "expected": "reachable"}

    def query(_api, *, query, limit):
        if "probe_success" in query:
            return [{"metric": labels, "value": 1}]
        if "duration_seconds" in query:
            samples = earlier if "offset" in query else [0.2, 0.2, 0.01]
            return [{"metric": labels, "values": list(enumerate(samples))}]
        return []

    monkeypatch.setattr(recorded, "prometheus_query_for", query)
    assert recorded.recorded_mesh(None, baselines={("a", "b"): 0.01})["anomalies"] == []


@pytest.mark.parametrize("budget_ms", [None, 2000])
def test_recorded_latency_uses_path_baseline_not_declared_wan_budget(
    monkeypatch, budget_ms
):
    labels = {"source": "a", "target": "b", "expected": "reachable"}

    def query(_api, *, query, limit):
        if "probe_success" in query:
            return [{"metric": labels, "value": 1}]
        if "duration_seconds" in query:
            return [{"metric": labels, "values": [[1, 0.2], [6, 0.2]]}]
        return []

    monkeypatch.setattr(recorded, "prometheus_query_for", query)
    intent = ProbeIntent(max_expected_rtt_ms=budget_ms)
    assert (
        recorded.recorded_mesh(None, intent, baselines={("a", "b"): 0.01})["anomalies"][
            0
        ]["type"]
        == "high_latency"
    )


def test_recorded_latency_reports_missing_path_baseline(monkeypatch):
    labels = {"source": "a", "target": "b", "expected": "reachable"}

    def query(_api, *, query, limit):
        if "probe_success" in query:
            return [{"metric": labels, "value": 1}]
        if "duration_seconds" in query:
            return [{"metric": labels, "values": [[1, 0.2], [2, 0.2]]}]
        return []

    monkeypatch.setattr(recorded, "prometheus_query_for", query)
    assert (
        recorded.recorded_mesh(None, baselines={})["anomalies"][0]["type"] == "unknown"
    )


def test_unavailable_latency_baseline_does_not_hide_unreachable_path(monkeypatch):
    labels = {"source": "a", "target": "b", "expected": "reachable", "module": "icmp"}

    def query(_api, *, query, limit):
        if "probe_success" in query:
            return [{"metric": labels, "value": 0}]
        if "duration_seconds" in query:
            return [{"metric": labels, "values": [[1, 0.2], [2, 0.2]]}]
        return []

    monkeypatch.setattr(recorded, "prometheus_query_for", query)
    anomalies = recorded.recorded_mesh(None, baselines={("a", "b"): None})["anomalies"]
    assert [row["type"] for row in anomalies] == ["unreachable"]


def test_recorded_latency_compares_each_direction_to_its_own_baseline(monkeypatch):
    def query(_api, *, query, limit):
        if "probe_success" in query:
            return [{"metric": {"source": "a", "target": "b"}, "value": 1}]
        if "duration_seconds" in query:
            return [
                {"metric": {"source": a, "target": b}, "values": [[1, 0.2], [2, 0.2]]}
                for a, b in [("a", "b"), ("b", "a")]
            ]
        return []

    monkeypatch.setattr(recorded, "prometheus_query_for", query)
    anomalies = recorded.recorded_mesh(
        None, baselines={("a", "b"): 0.01, ("b", "a"): 0.2}
    )["anomalies"]
    assert [(a["source"], a["target"], a["type"]) for a in anomalies] == [
        ("a", "b", "high_latency")
    ]
