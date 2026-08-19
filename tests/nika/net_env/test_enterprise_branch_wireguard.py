"""Unit tests for enterprise_branch WireGuard helpers and peer-key targets."""

from __future__ import annotations

from nika.net_env.kathara.enterprise_wan.enterprise_branch.topology import (
    single_path_hq_peer_targets,
)
from nika.net_env.kathara.enterprise_wan.enterprise_branch.wireguard import (
    load_key_pairs,
)
from nika.problems.misconfigurations.wireguard import WRONG_HUB_PEER_PUBLIC_KEY


def test_wrong_hub_peer_key_is_not_a_lab_edge_key() -> None:
    used = {pub for _, pub in load_key_pairs()[:9]}
    assert WRONG_HUB_PEER_PUBLIC_KEY not in used


def test_single_path_hq_peer_targets_by_size() -> None:
    assert single_path_hq_peer_targets("s") == [
        ("br1_edge", "wg_hq"),
        ("br2_edge", "wg_hq"),
    ]
    assert single_path_hq_peer_targets("m") == [
        ("br3_edge", "wg_hq"),
        ("br4_edge", "wg_hq"),
    ]
    assert single_path_hq_peer_targets("l") == [
        ("br4_edge", "wg_hq"),
        ("br5_edge", "wg_hq"),
        ("br6_edge", "wg_hq"),
        ("br7_edge", "wg_hq"),
    ]
