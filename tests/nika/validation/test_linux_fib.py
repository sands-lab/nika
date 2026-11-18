"""Installed Linux FIBs become NetKAT forwarding rules."""

import pytest

from nika.validation.netkat.linux_fib import fib_network

pytestmark = pytest.mark.unit


def _addr(*cidrs):
    return [
        {
            "addr_info": [
                {
                    "family": "inet",
                    "local": c.split("/")[0],
                    "prefixlen": int(c.split("/")[1]),
                }
                for c in cidrs
            ]
        }
    ]


STATE = {
    "r1": {
        "addr": _addr("10.0.0.0/31", "10.0.1.0/31", "192.0.2.1/24"),
        "route": [
            {"dst": "192.0.2.0/24", "dev": "eth2", "scope": "link"},
            {
                "dst": "198.51.100.0/24",
                "nexthops": [{"gateway": "10.0.0.1"}, {"gateway": "10.0.1.1"}],
            },
            {"dst": "203.0.113.0/24", "type": "blackhole"},
        ],
    },
    "r2": {
        "addr": _addr("10.0.0.1/31", "10.0.1.1/31", "198.51.100.1/24"),
        "route": [
            {"dst": "198.51.100.0/24", "dev": "eth2", "scope": "link"},
            {"dst": "default", "gateway": "10.0.0.0", "metric": 20},
            {"dst": "default", "gateway": "10.0.1.0", "metric": 10},
        ],
    },
    "h1": {
        "addr": _addr("192.0.2.10/24"),
        "route": [{"dst": "default", "gateway": "192.0.2.1"}],
    },
    "h2": {
        "addr": _addr("198.51.100.10/24"),
        "route": [{"dst": "default", "gateway": "198.51.100.1"}],
    },
}


def test_routes_become_longest_prefix_rules_to_owning_nodes():
    network = fib_network(
        STATE, {"r1", "r2"}, {"h1": "192.0.2.10", "h2": "198.51.100.10"}
    )
    assert network.endpoints["h1"].switch == "r1"
    rules = {r.prefix: r for r in network.rules["r1"]}
    assert [a.peer for a in rules["198.51.100.0/24"].actions] == ["r2"]
    assert rules["203.0.113.0/24"].actions == ()
    assert [a.peer for a in rules["192.0.2.10/32"].actions] == ["h1"]
    # The lower metric wins among routes for one prefix.
    (default,) = [r for r in network.rules["r2"] if r.prefix == "0.0.0.0/0"]
    assert [a.peer for a in default.actions] == ["r1"]
    network.relation()


def test_a_host_needs_one_gateway_router():
    state = {**STATE, "h1": {"addr": _addr("192.0.2.10/24"), "route": []}}
    with pytest.raises(ValueError, match="default gateway"):
        fib_network(state, {"r1", "r2"}, {"h1": "192.0.2.10"})


def test_router_traffic_uses_a_local_endpoint_on_that_router():
    network = fib_network(
        STATE, {"r1", "r2"}, {"r2:local": "10.0.0.1"}, {"r2:local": "r2"}
    )
    assert network.endpoints["r2:local"].switch == "r2"
    own = {r.prefix: r for r in network.rules["r2"] if r.priority == 66}
    assert [a.peer for a in own["10.0.0.1/32"].actions] == ["r2:local"]
    network.relation()
