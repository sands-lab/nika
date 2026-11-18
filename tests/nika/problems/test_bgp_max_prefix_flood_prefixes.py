"""bgp_max_prefix_exceeded floods a reproducible, scattered set of more-specifics."""

from __future__ import annotations

import pytest

from nika.problems.routing_control_plane.bgp import BGPMaxPrefixExceeded

pytestmark = pytest.mark.unit


def test_flood_prefixes_are_reproducible_and_scattered():
    problem = BGPMaxPrefixExceeded(None)
    prefixes = problem._flood_prefixes(40, peer="AS65002", neighbor_ip="10.0.0.2")
    assert prefixes == problem._flood_prefixes(
        40, peer="AS65002", neighbor_ip="10.0.0.2"
    )
    assert prefixes != problem._flood_prefixes(
        40, peer="AS65003", neighbor_ip="10.0.0.3"
    )
    assert len(set(prefixes)) == 40
    assert all(p.startswith("198.19.") and p.endswith(".0/24") for p in prefixes)
    assert sorted(int(p.split(".")[2]) for p in prefixes) != list(range(40))
