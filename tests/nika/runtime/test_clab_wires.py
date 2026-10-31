"""Containerlab node-to-node links are rewritten through host veth ends."""

from __future__ import annotations

import pytest

from nika.runtime.containerlab.wires import wire_links

pytestmark = pytest.mark.unit


def test_wire_links_splits_node_pairs_and_leaves_other_links() -> None:
    host_mgmt = {"endpoints": ["leaf1:eth3", "host:mgmt0"]}
    dangling = {"endpoints": ["leaf1:eth4"]}
    document = {
        "name": "demo",
        "topology": {
            "kinds": {"linux": {"image": "alpine"}},
            "nodes": {
                "leaf1": {"kind": "linux"},
                "spine": {"kind": "linux"},
                "client": {"kind": "linux"},
            },
            "links": [
                {"endpoints": ["leaf1:eth1", "spine:eth1"], "mtu": 9100},
                {"endpoints": ["leaf1:eth2", "client:eth1"]},
                host_mgmt,
                dangling,
            ],
        },
    }

    wired, wires = wire_links(document, "demo")

    assert wired["name"] == "demo"
    assert wired["topology"]["kinds"] == document["topology"]["kinds"]
    assert wired["topology"]["nodes"] == document["topology"]["nodes"]
    assert host_mgmt in wired["topology"]["links"]
    assert dangling in wired["topology"]["links"]

    host_links = [
        link for link in wired["topology"]["links"] if link not in (host_mgmt, dangling)
    ]
    assert len(host_links) == 4
    assert len(wires) == 2
    mtu_links = [link for link in host_links if link.get("mtu") == 9100]
    assert len(mtu_links) == 2
    assert all("mtu" not in link for link in host_links if link not in mtu_links)

    host_ends = [end for pair in wires for end in pair]
    assert len(host_ends) == len(set(host_ends))
    assert all(len(name) <= 15 for name in host_ends)

    by_node_end = {}
    for link in host_links:
        node_end, host_end = link["endpoints"]
        assert host_end.startswith("host:")
        by_node_end[node_end] = host_end.split(":", 1)[1]
    assert by_node_end["leaf1:eth1"] != by_node_end["spine:eth1"]
    assert {by_node_end["leaf1:eth1"], by_node_end["spine:eth1"]} == set(wires[0])
    assert {by_node_end["leaf1:eth2"], by_node_end["client:eth1"]} == set(wires[1])
