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


def test_srl_hijack_uses_best_path_origin_beyond_a_transit_next_hop(monkeypatch):
    import json

    problem = _problem("nokia_srlinux", "")
    problem.runtime.list_nodes = lambda: []
    attrs = {
        "index": "42",
        "next-hop": "192.0.2.1",
        "as-path": {
            "segment": [
                {"as-path-index": 1, "type": "as-sequence", "member": [65001, 65003]}
            ]
        },
    }
    rib = {
        "attr-sets": {"attr-set": [attrs]},
        "afi-safi": [
            {
                "afi-safi-name": "srl_nokia-common:ipv4-unicast",
                "ipv4-unicast": {
                    "local-rib": {
                        "route": [
                            {
                                "prefix": "203.0.113.0/24",
                                "attr-id": "42",
                                "best-route": True,
                            }
                        ]
                    }
                },
            }
        ],
    }
    problem.runtime.lab_api = SimpleNamespace(
        srl_exec_cli=lambda *_: json.dumps(
            {"network-instance": [{"name": "default", "bgp-rib": rib}]}
        ),
        srl_get_bgp_as=lambda node: 65001 if node == "origin" else 65002,
    )
    params = SimpleNamespace(
        host_name="origin",
        symptom_host="observer",
        target_network="203.0.113.0/24",
        observer_host=None,
        probe_dst_ip="203.0.113.1",
    )
    monkeypatch.setattr(probes, "_router_addresses", lambda *_: {"192.0.2.1"})
    # Forwarding through the hijacker does not prove it originated the route.
    reading = probes._hijack_reading(problem, params)
    assert reading["best_via_hijacker"] is False
    attrs["as-path"]["segment"][0]["member"] = [65001]
    assert probes._hijack_reading(problem, params)["best_via_hijacker"] is True
    rib["afi-safi"][0]["ipv4-unicast"]["local-rib"]["route"][0]["best-route"] = False
    assert probes._hijack_reading(problem, params)["best_via_hijacker"] is False
    problem.runtime.lab_api.srl_exec_cli = lambda *_: "Parsing error"
    ok, reading = probes.hijack_baseline(problem, params)
    assert not ok
    assert reading["error"] == "bgp_query_failed_on_observer"
