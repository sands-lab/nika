"""BGP audit reads distinguish missing routes from failed observations."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika.validation.symptom import bgp_route_probes as probes

pytestmark = pytest.mark.unit


def _problem(profile, output):
    return SimpleNamespace(
        net_env=SimpleNamespace(
            machine_identities={
                node: SimpleNamespace(capabilities=(profile, "bgp"))
                for node in ("origin", "observer")
            }
        ),
        runtime=SimpleNamespace(exec=Mock(return_value=output)),
        root_cause_name="test",
    )


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
@pytest.mark.parametrize(
    "output", ["", "[TIMEOUT]", "% Invalid input", "bad command name"]
)
def test_failed_route_reads_are_unreadable(profile, output):
    problem = _problem(profile, output)
    assert probes.prefix_paths(problem, "observer", "203.0.113.0/24") is None
    assert probes._table(problem, "observer") is None
    assert probes._local_asn(problem, "observer") is None


def test_partial_advertisement_baseline_cannot_hide_an_unreadable_observer(monkeypatch):
    problem = _problem("frr", "")
    params = SimpleNamespace(host_name="origin", symptom_host=None, prefix=None)
    monkeypatch.setattr(
        probes, "bgp_speakers_of", lambda *_: ["observer", "unreadable"]
    )
    monkeypatch.setattr(probes, "_router_addresses", lambda *_: {"192.0.2.1"})
    monkeypatch.setattr(probes, "_local_asn", lambda *_: 65001)
    monkeypatch.setattr(
        probes,
        "_advertised_by",
        lambda _, node, *args: ["203.0.113.0/24"] if node == "observer" else None,
    )
    ok, details = probes.advertisement_baseline(problem, params)
    assert not ok
    assert details["errors"] == ["bgp_query_failed_on_unreadable"]
    assert problem._audit_advertised is None
    assert not probes.advertisement_withdrawn(problem, params)[0]


def test_unreadable_withdrawal_is_not_a_lost_route():
    problem = _problem("routeros", "failure: command failed")
    problem._audit_advertised = {
        "host": "origin",
        "addresses": ["192.0.2.1"],
        "asn": 65001,
        "seen": {"observer": ["203.0.113.0/24"]},
    }
    ok, result = probes.advertisement_withdrawn(
        problem, SimpleNamespace(host_name="origin", symptom_host=None)
    )
    assert not ok
    assert result["details"]["errors"] == ["bgp_query_failed_on_observer"]


def test_unreadable_hijacker_identity_is_not_a_healthy_baseline(monkeypatch):
    problem = _problem("iosxr", "")
    problem.runtime.list_nodes = lambda: []
    params = SimpleNamespace(
        host_name="origin",
        symptom_host="observer",
        target_network="203.0.113.0/24",
        observer_host=None,
    )
    monkeypatch.setattr(probes, "_router_addresses", lambda *_: {"192.0.2.1"})
    monkeypatch.setattr(probes, "prefix_paths", lambda *_: [])
    ok, reading = probes.hijack_baseline(problem, params)
    assert not ok
    assert reading["error"] == "bgp_query_failed_on_origin"


def test_unreadable_rpki_observer_is_not_rejection():
    problem = _problem("routeros", "failure: command failed")
    problem._rpki_bgp_inventory = lambda: {
        "leaker_asn": 65001,
        "rov_observer": "origin",
        "non_rov_observer": "observer",
        "leak_prefixes": ["203.0.113.0/24"],
    }
    assert not probes.rpki_leak_baseline(problem, None)[0]
    ok, result = probes.rpki_leak_split(problem, None)
    assert not ok
    assert result["details"]["error"] == "bgp_query_failed_on_observer"
