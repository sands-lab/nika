"""BGP audit reads distinguish missing routes from failed observations."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika.validation.symptom import bgp_route_probes as probes

pytestmark = pytest.mark.unit


def test_peer_scoped_policy_baseline_supports_transit_routers(monkeypatch):
    from nika.runtime.spec import NodeIdentity, NodeRole
    from nika.validation.symptom import addressing_probes

    links = [
        ("core:eth0", "edge:eth0"),
        ("core:eth1", "remote:eth0"),
        ("edge:eth1", "server:eth0"),
        ("remote:eth1", "client:eth0"),
    ]
    env = SimpleNamespace(
        inventory={
            "links": [
                {
                    key: dict(zip(("device", "iface"), endpoint.split(":")))
                    for key, endpoint in zip(("endpoint_a", "endpoint_b"), link)
                }
                for link in links
            ]
        },
        machine_identities={
            node: NodeIdentity(
                role=role, service_type="web" if role is NodeRole.SERVICE else None
            )
            for node, role in {
                "core": NodeRole.ROUTER,
                "edge": NodeRole.ROUTER,
                "remote": NodeRole.ROUTER,
                "server": NodeRole.SERVICE,
                "client": NodeRole.HOST,
            }.items()
        },
    )
    problem = SimpleNamespace(
        net_env=env,
        runtime=object(),
        scenario_name="example",
        session_interfaces=lambda *_: {"192.0.2.0": "eth0"},
    )
    monkeypatch.setattr(
        probes,
        "get_probe_path",
        lambda *a, **k: SimpleNamespace(src_host="client", dst_ip="10.0.1.2"),
    )
    monkeypatch.setattr(
        addressing_probes,
        "lab_addresses",
        lambda _: {
            "server": [("eth0", "10.0.1.2/24")],
            "client": [("eth0", "10.0.2.1/24")],
        },
    )
    assert probes._policy_probe_ends(problem, "core", "192.0.2.0") == {
        "src": "client",
        "dst_host": "server",
        "dst": "10.0.1.2",
    }
    # An unscoped or undeclared peer must not choose an unrelated path.
    for peer in (None, "192.0.2.99"):
        assert probes._policy_probe_ends(problem, "core", peer) == {
            "error": "no_end_host_behind_router"
        }


@pytest.mark.parametrize("peer", [None, "192.0.2.1"])
@pytest.mark.parametrize(
    ("received", "session_down", "loss", "all_expected", "scoped_expected"),
    [
        ({"192.0.2.1": 0, "192.0.2.2": 0}, False, 0.0, True, True),
        ({"192.0.2.1": 3, "192.0.2.2": 2}, False, 100.0, False, False),
        ({"192.0.2.1": 0, "192.0.2.2": 0}, True, 100.0, False, False),
        ({"192.0.2.1": 0, "192.0.2.2": 2}, False, 0.0, False, True),
    ],
)
def test_session_policy_checks_route_exchange_on_affected_peers(
    monkeypatch, peer, received, session_down, loss, all_expected, scoped_expected
):
    from nika.problems.support import router_ops

    state = SimpleNamespace(
        received={"192.0.2.1": 3, "192.0.2.2": 2},
        peers={"192.0.2.1", "192.0.2.2", "192.0.2.3"},
        loss=0.0,
    )
    ops = SimpleNamespace(
        bgp_established_peers=lambda: state.peers,
        bgp_accepted_prefix_count=lambda address: state.received[address],
    )
    problem = SimpleNamespace(
        net_env=object(),
        runtime=object(),
        root_cause_name="bgp_session_policy_missing",
        session_interfaces=lambda *_: {"192.0.2.1": "eth0", "192.0.2.2": "eth1"},
    )
    params = SimpleNamespace(host_name="border", peer=peer)
    monkeypatch.setattr(router_ops, "router_fault_ops", lambda *_: ops)
    monkeypatch.setattr(
        probes, "_policy_probe_ends", lambda *_: {"src": "client", "dst": "10.0.0.1"}
    )
    monkeypatch.setattr(
        probes, "ping_stats", lambda *a, **k: SimpleNamespace(loss_percent=state.loss)
    )
    monkeypatch.setattr(probes, "_ROUTE_WAIT_SEC", 0)
    assert probes.session_policy_baseline(problem, params)[0]
    # Healthy sessions and routes must not satisfy the symptom.
    assert not probes.session_policy_drops_routes(problem, params)[0]
    state.received, state.loss = received, loss
    if session_down:
        state.peers.remove("192.0.2.1")
    ok, _ = probes.session_policy_drops_routes(problem, params)
    assert ok is (scoped_expected if peer else all_expected)


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


def test_metallb_routes_are_read_in_the_frr_pod_container():
    import json

    problem = _problem("frr", "")
    problem.runtime.lab_api = SimpleNamespace(
        kubectl=Mock(
            return_value=SimpleNamespace(
                stdout=json.dumps(
                    {
                        "routes": {
                            "203.0.113.0/24": [{"path": "65001", "bestpath": True}]
                        }
                    }
                )
            )
        )
    )
    router = "pod:controller/metallb-system/speaker-abc"
    table = probes._table(problem, router)
    assert table["203.0.113.0/24"][0]["aspath"] == "65001"
    problem.runtime.lab_api.kubectl.assert_called_once_with(
        "controller",
        "exec -n metallb-system speaker-abc -c frr -- vtysh -c 'show bgp ipv4 unicast json'",
        check=False,
    )
    problem.runtime.exec.assert_not_called()
    problem.runtime.lab_api.kubectl.return_value.stdout = "{}"
    assert probes._advertised_by(problem, router, {"192.0.2.1"}, 65001) == []
    problem.runtime.lab_api.kubectl.return_value.stdout = "[TIMEOUT]"
    assert probes._table(problem, router) is None


@pytest.mark.parametrize("neighbors,healthy", [({}, True), (None, False)])
def test_advertisement_observer_needs_configured_bgp_sessions(
    monkeypatch, neighbors, healthy
):
    problem = _problem("frr", "")
    params = SimpleNamespace(host_name="origin", symptom_host=None, prefix=None)
    pod = "pod:controller/metallb-system/speaker-abc"
    monkeypatch.setattr(probes, "bgp_speakers_of", lambda *_: ["observer", pod])
    monkeypatch.setattr(probes, "bgp_neighbors", lambda *_: neighbors)
    monkeypatch.setattr(probes, "_router_addresses", lambda *_: {"192.0.2.1"})
    monkeypatch.setattr(probes, "_local_asn", lambda *_: 65001)
    monkeypatch.setattr(
        probes,
        "_advertised_by",
        lambda _, node, *args: ["203.0.113.0/24"] if node == "observer" else None,
    )
    assert probes.advertisement_baseline(problem, params)[0] is healthy


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


def test_network_withdrawal_does_not_require_redistributed_loopbacks_to_disappear(
    monkeypatch,
):
    problem = _problem("frr", "")
    params = SimpleNamespace(host_name="origin", symptom_host="observer", prefix=None)
    problem.withdrawal_prefixes = lambda _: {"203.0.113.0/24"}
    monkeypatch.setattr(probes, "_router_addresses", lambda *_: {"192.0.2.1"})
    monkeypatch.setattr(probes, "_local_asn", lambda *_: 65001)
    learned = ["192.0.2.1/32", "203.0.113.0/24"]
    monkeypatch.setattr(probes, "_advertised_by", lambda *_: learned)
    assert probes.advertisement_baseline(problem, params)[0]
    assert problem._audit_advertised["seen"] == {"observer": ["203.0.113.0/24"]}
    learned[:] = ["192.0.2.1/32"]
    assert probes.advertisement_withdrawn(problem, params)[0]


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


@pytest.mark.parametrize("profile", ["frr", "sonic"])
def test_frr_platform_route_reads_preserve_origin_and_best_path(profile):
    import json

    prefix = "203.0.113.0/24"
    hop = {"ip": "192.0.2.1"}
    problem = _problem(
        profile,
        json.dumps(
            {
                "paths": [
                    {
                        "aspath": {"string": "65003"},
                        "bestpath": {"overall": True},
                        "nexthops": [hop],
                    }
                ]
            }
        ),
    )
    assert probes.prefix_paths(problem, "observer", prefix) == [
        {
            "aspath": "65003",
            "best": True,
            "nexthops": ["192.0.2.1"],
            "originator": None,
            "peer_router_id": None,
        }
    ]
    problem.runtime.exec.return_value = json.dumps(
        {"routes": {prefix: [{"path": "65003", "bestpath": True, "nexthops": [hop]}]}}
    )
    assert probes._table(problem, "observer")[prefix][0]["aspath"] == "65003"
    assert probes._table(problem, "observer")[prefix][0]["best"]


@pytest.mark.parametrize("profile", ["frr", "sonic"])
def test_bgp_observers_are_read_when_igps_are_not_running(profile):
    import json

    from nika.validation.symptom.routing_probes import _read_view

    problem = _problem(profile, "")
    problem.runtime.backend = "containerlab" if profile == "sonic" else "kathara"
    problem.runtime.exec.side_effect = lambda _, cmd, **kwargs: (
        json.dumps({"default": {"192.0.2.1": {"bgpState": "Established"}}})
        if "show bgp" in cmd
        else ""
    )
    plan = {
        "host_addresses": ["192.0.2.1"],
        "facing": {},
        "ospf": ["observer"],
        "isis": ["observer"],
        "bgp": ["observer"],
    }
    states, _, _ = _read_view(problem, "origin", plan)
    assert states == {"bgp|observer|192.0.2.1": "Established"}
