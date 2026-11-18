"""Design changes of negative controls edit netlab's model, not rendered text."""

import pytest

from nika.net_env.synthesis.changes import (
    DesignChange,
    apply_input_changes,
    apply_model_changes,
)

pytestmark = pytest.mark.unit


def _model():
    return {
        "nodes": {
            "r1": {
                "loopback": {"ipv4": "10.255.0.1/32", "ospf": {"area": "0.0.0.0"}},
                "interfaces": [
                    {
                        "ipv4": "10.0.0.0/31",
                        "ospf": {"area": "0.0.0.1"},
                        "bgp": {"advertise": False},
                    },
                    {"ipv4": "198.51.100.1/24", "bgp": {"advertise": True}},
                ],
                "bgp": {
                    "as": 65001,
                    "neighbors": [
                        {
                            "ipv4": "10.0.0.1",
                            "as": 65002,
                            "policy": {"out": "OUT"},
                        },
                        {"ipv4": "10.255.0.2", "as": 65001, "rr_client": True},
                    ],
                },
                "routing": {
                    "prefix": {"BUSINESS": [{"ipv4": ["198.51.100.0/24"]}]},
                    "policy": {
                        "OUT": [{"action": "permit", "match": {"prefix": "BUSINESS"}}]
                    },
                },
            }
        }
    }


def test_changes_set_the_designed_value_they_name():
    model = _model()
    apply_model_changes(
        model,
        [
            DesignChange(kind="peer_asn", node="r1", peer="10.0.0.1", asn=65099),
            DesignChange(kind="local_asn", node="r1", asn=65100),
            DesignChange(kind="rr_client", node="r1", peer="10.255.0.2"),
            DesignChange(kind="deny_export", node="r1", prefix="198.51.100.0/24"),
            DesignChange(kind="withdraw_origin", node="r1", prefix="198.51.100.0/24"),
            DesignChange(
                kind="interface_area", node="r1", prefix="10.0.0.0/31", area=66
            ),
            DesignChange(kind="interface_area", node="r1", prefix="10.255.0.1/32"),
        ],
    )
    r1 = model["nodes"]["r1"]
    peer, client = r1["bgp"]["neighbors"]
    assert (peer["as"], r1["bgp"]["as"], "rr_client" in client) == (65099, 65100, False)
    assert r1["routing"]["policy"]["OUT"][0]["action"] == "deny"
    assert r1["interfaces"][1]["bgp"]["advertise"] is False
    assert r1["interfaces"][0]["ospf"]["area"] == "0.0.0.66"
    assert "ospf" not in r1["loopback"]


def test_a_change_that_matches_nothing_is_rejected():
    with pytest.raises(ValueError, match="matches nothing"):
        apply_model_changes(
            _model(), [DesignChange(kind="peer_asn", node="r1", peer="10.9.9.9", asn=1)]
        )
    with pytest.raises(ValueError, match="requires"):
        DesignChange(kind="peer_asn", node="r1", peer="10.0.0.1")


def test_discard_route_enables_netlab_static_routing():
    topology = {"module": ["ospf"], "nodes": {"r1": {}}}
    apply_input_changes(
        topology,
        [DesignChange(kind="discard_route", node="r1", prefix="10.200.0.0/24")],
    )
    assert topology["nodes"]["r1"] == {
        "module": ["ospf", "routing"],
        "routing.static": [{"ipv4": "10.200.0.0/24", "nexthop": {"discard": True}}],
    }
