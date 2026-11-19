"""Duplicate IDs require repeated loss of routes learned before injection."""

from types import SimpleNamespace

import pytest

from nika.validation.symptom import routing_probes as probes

pytestmark = pytest.mark.unit


def _run(monkeypatch, missing, router_id="10.255.0.2"):
    now = [0.0]
    monkeypatch.setattr(probes.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        probes.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    monkeypatch.setattr(probes, "_DUPLICATE_ID_WAIT_SEC", 15.0)
    monkeypatch.setattr(probes, "_ospf_router_id", lambda *_: router_id)
    prefixes = {"10.254.0.0/30", "10.254.0.8/30"}
    monkeypatch.setattr(
        probes, "_ospf_routes", lambda _, observer: prefixes - missing(observer, now[0])
    )
    problem = SimpleNamespace(
        root_cause_name="ospf_duplicate_router_id",
        _audit_ospf_stubs={
            "stubs": {
                "r1": {"10.254.0.0/30": ["observer1", "observer2"]},
                "r2": {"10.254.0.8/30": ["observer1", "observer2"]},
            },
            "router_ids": {"r1": "10.255.0.1", "r2": "10.255.0.2"},
        },
    )
    return probes.ospf_stubs_withdrawn(problem, SimpleNamespace(host_name="r1"))


def test_persistent_route_loss_at_one_observer(monkeypatch):
    ok, result = _run(
        monkeypatch,
        lambda observer, _: {"10.254.0.0/30"} if observer == "observer1" else set(),
    )
    assert ok
    assert result["details"]["withdrawn_routes"] == [
        {"owner": "r1", "prefix": "10.254.0.0/30", "observer": "observer1"}
    ]
    assert result["details"]["observations"][-1]["elapsed_sec"] >= 10


def test_alternating_lsa_winners_keep_routes_unstable(monkeypatch):
    def missing(observer, seconds):
        if observer == "observer2":
            return set()
        return {"10.254.0.0/30" if int(seconds / 3) % 2 else "10.254.0.8/30"}

    ok, result = _run(monkeypatch, missing)
    assert ok
    assert {
        row["owner"]
        for reading in result["details"]["observations"]
        for row in reading["withdrawn_routes"]
    } == {"r1", "r2"}


def test_duplicate_id_without_route_loss_is_not_a_symptom(monkeypatch):
    ok, _ = _run(monkeypatch, lambda *_: set())
    assert not ok


def test_one_transient_route_withdrawal_is_not_persistent(monkeypatch):
    ok, _ = _run(
        monkeypatch,
        lambda observer, seconds: (
            {"10.254.0.0/30"} if observer == "observer1" and seconds == 0 else set()
        ),
    )
    assert not ok


def test_unique_router_id_is_not_a_duplicate_symptom(monkeypatch):
    ok, _ = _run(monkeypatch, lambda *_: {"10.254.0.0/30"}, router_id="10.255.0.1")
    assert not ok


def test_unreadable_router_id_is_not_a_healthy_observation(monkeypatch):
    with pytest.raises(ValueError, match="router ID"):
        _run(monkeypatch, lambda *_: set(), router_id=None)
