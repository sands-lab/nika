"""Forwarding equivalence must account for resolved ECMP and route ownership."""

import json

import pytest

from nika.problems.forwarding_encapsulation_policy.rib_fib import (
    divergent_routes,
    rib_routes,
)

pytestmark = pytest.mark.unit


def test_resolved_ecmp_divergence_excludes_other_route_causes():
    prefix = "10.0.1.0/24"
    hops = [
        {"ip": "172.16.0.1", "interfaceName": "eth0", "active": True},
        {"ip": "172.16.0.3", "interfaceName": "eth1", "active": True},
    ]
    rib = rib_routes(
        json.dumps(
            {
                prefix: [
                    {
                        "protocol": "bgp",
                        "selected": True,
                        "table": 254,
                        "nexthops": [
                            {
                                "ip": "10.255.0.1",
                                "recursive": True,
                                "active": True,
                                "resolvedVia": hops,
                            }
                        ],
                    }
                ]
            }
        )
    )
    route = {
        "dst": prefix,
        "protocol": "bgp",
        "nhid": 11,
        "metric": 20,
        "nexthops": [
            {"gateway": h["ip"], "dev": h["interfaceName"]} for h in reversed(hops)
        ],
    }
    assert divergent_routes(rib, [route]) == {}
    stale = {
        "dst": prefix,
        "protocol": "bgp",
        "metric": 20,
        "gateway": "192.168.0.2",
        "dev": "eth2",
    }
    assert divergent_routes(rib, [stale])[prefix] == {
        "rib": [["172.16.0.1", "eth0"], ["172.16.0.3", "eth1"]],
        "fib": [["192.168.0.2", "eth2"]],
    }
    assert prefix in divergent_routes(rib, [])
    assert divergent_routes(rib, [{**stale, "protocol": "static"}]) == {}
    assert divergent_routes(rib, [{"dst": prefix, "protocol": "bgp", "nhid": 11}]) == {}
    assert divergent_routes(rib, [route, {**stale, "table": 100}]) == {}
    # Static, unselected, and unresolved routes do not supply a dynamic best path.
    for changes in ({"protocol": "static"}, {"selected": False}, {"nexthops": []}):
        assert (
            rib_routes(
                json.dumps(
                    {
                        prefix: [
                            {
                                "protocol": "bgp",
                                "selected": True,
                                "nexthops": hops,
                                **changes,
                            }
                        ]
                    }
                )
            )
            == {}
        )
