"""Backend-neutral runtime helpers: exec output, link neighbors, peer lookup."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

from nika.net_env.verify import frr_bgp_established_peers
from nika.runtime.containerlab.runtime import ContainerlabRuntime
from nika.runtime.shared.execution import merge_exec_output
from nika.service.containerlab.host_tc import host_veth_for
from nika.service.kathara.docker_utils import link_neighbors


def test_exec_output_merges_streams_without_markers() -> None:
    assert merge_exec_output(b" out\n", b"err\n") == "out\nerr"
    assert merge_exec_output(b"", b"ping: unknown host") == "ping: unknown host"
    assert merge_exec_output(None, None) == ""


def test_bgp_established_peers_accepts_numbered_and_unnumbered() -> None:
    summary = (
        "Neighbor        V   AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down "
        "State/PfxRcd PfxSnt Desc\n"
        "10.0.0.2        4 65002     25       9      6   0    0 00:00:30 "
        "           5      1 N/A\n"
        "10.0.0.6        4 65003      0       0      0   0    0    never "
        "      Active      0 N/A\n"
        "spine1(eth1)    4 65100     12      12      0   0    0 00:01:00 "
        "           0      3 N/A\n"
        "Total number of neighbors 3\n"
    )
    assert frr_bgp_established_peers(summary) == {"10.0.0.2", "spine1(eth1)"}


def _link(name: str, *members: str) -> SimpleNamespace:
    containers = [SimpleNamespace(labels={"name": member}) for member in members]
    return SimpleNamespace(name=name, containers=containers)


def test_link_neighbors_handles_dangling_and_shared_links() -> None:
    links = [_link("A", "r1", "r2"), _link("B", "r1"), _link("C", "r1", "h1", "h2")]
    assert link_neighbors(links, "r1") == ["r2", "h1", "h2"]
    assert link_neighbors(links, "h1") == ["r1", "h2"]


def test_host_veth_requires_matching_peer_index_and_other_namespace() -> None:
    output = (
        "7: eth9: <BROADCAST,UP> mtu 1500 qdisc mq state UP\\    link/ether aa\n"
        "41: veth1a@if40: <BROADCAST,UP> mtu 1500 master br0\\    link/ether bb "
        "link-netnsid 3\n"
        "42: veth2b@if9: <BROADCAST,UP> mtu 1500\\    link/ether cc link-netnsid 4\n"
    )
    assert host_veth_for(output, "41", "40") == "veth1a"
    # Same host ifindex, but its peer is a different interface.
    assert host_veth_for(output, "42", "40") is None
    # A bare ifindex match on a non-veth host interface is not the peer.
    assert host_veth_for(output, "7", "40") is None


def test_clab_node_map_survives_transient_inspect_failure(monkeypatch) -> None:
    runtime = ContainerlabRuntime(lab_name="lab", topology_file="/tmp/topo.yml")
    container = MagicMock()
    runtime._node_containers = {"r1": container}
    failed = subprocess.CompletedProcess(["clab"], 1, stdout="", stderr="busy")
    monkeypatch.setattr(runtime, "_run_clab", lambda *args: failed)

    assert runtime._refresh_node_map() is False
    assert runtime._cached_container("r1") is container
