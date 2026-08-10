"""Unit tests for topology-aware mock agent helpers."""

from __future__ import annotations

from agent.mock.mock_agent import (
    _mock_diagnosis_tool_calls,
    _pick_pair,
    _pick_router,
)


def test_pick_pair_prefers_ground_truth_devices() -> None:
    pair = _pick_pair(
        ["pc_2_1_1_1", "router_dist_2_1", "pc_2_1_1_2"],
        ["pc_2_1_1_1"],
    )
    assert pair == ("pc_2_1_1_1", "pc_2_1_1_1")


def test_pick_pair_uses_two_lab_devices() -> None:
    pair = _pick_pair(["web_server_1_1", "vpn_server_1", "pc1"], [])
    assert pair == ("web_server_1_1", "vpn_server_1")


def test_pick_router_from_clos_names() -> None:
    assert _pick_router(["pc1", "leaf_router_0_1", "spine_0"]) == "leaf_router_0_1"


def test_diagnosis_calls_use_lab_hosts_not_hardcoded_pc() -> None:
    calls = _mock_diagnosis_tool_calls(
        backend="kathara",
        server_names=["kathara_base_mcp_server", "kathara_frr_mcp_server"],
        devices=["pc_2_1_1_1", "router_dist_2_1", "pc_2_1_1_2"],
        preferred=["pc_2_1_1_1"],
    )
    by_name = {name: args for name, args in calls}
    assert "get_reachability" in by_name
    assert by_name["ping_pair"]["host_a"] == "pc_2_1_1_1"
    assert by_name["ping_pair"]["host_b"] == "pc_2_1_1_1"
    assert by_name["frr_show_ip_route"]["router_name"] == "router_dist_2_1"
    assert "pc1" not in str(calls)
    assert "pc2" not in str(calls)
