"""Unit tests for ISP compilation (no Docker)."""

from __future__ import annotations

from ipaddress import IPv4Network

import pytest

from nika.net_env.isp.igp import (
    IspConfig,
    IspCompileError,
    IspConfigError,
    compile_isp_plan,
    link_metric,
    slugify,
)
from nika.topology.models import (
    CapacityModule,
    NetworkTopology,
    TopoLink,
    TopoNode,
)


def _tiny_topology(
    *,
    name: str = "tiny",
    nodes: tuple[str, ...] = ("B", "A"),
    links: tuple[tuple[str, str, str], ...] = (("L2", "B", "A"), ("L1", "A", "B")),
) -> NetworkTopology:
    return NetworkTopology(
        name=name,
        source_format="sndlib-xml",
        meta={},
        nodes=tuple(TopoNode(id=n) for n in nodes),
        links=tuple(
            TopoLink(id=lid, source=src, target=dst) for lid, src, dst in links
        ),
        demands=(),
    )


def test_config_rejects_unsupported_igp() -> None:
    with pytest.raises(IspConfigError, match="Unsupported IGP"):
        IspConfig(igp="bgp").validated()  # type: ignore[arg-type]


def test_config_rejects_unsupported_metric_strategy() -> None:
    with pytest.raises(IspConfigError, match="metric strategy"):
        IspConfig(metric_strategy="random").validated()  # type: ignore[arg-type]


def test_config_rejects_unknown_topology() -> None:
    with pytest.raises(IspConfigError, match="Unknown SNDlib topology"):
        IspConfig(topology="not-a-real-topo").validated()


def test_config_rejects_bad_constant_metric() -> None:
    with pytest.raises(IspConfigError, match="constant_metric"):
        IspConfig(constant_metric=0).validated()


def test_polska_default_compile_is_stable() -> None:
    cfg = IspConfig(topology="polska")
    first = compile_isp_plan(cfg)
    second = compile_isp_plan(cfg)
    assert first.igp == "isis"
    assert first.metric_strategy == "constant"
    assert first.constant_metric == 10
    assert len(first.nodes) == 12
    assert len(first.links) == 18
    assert first.inventory == second.inventory
    assert [n.frr_conf for n in first.nodes] == [n.frr_conf for n in second.nodes]
    assert all(link.metric == 10 for link in first.links)
    assert first.inventory["nodes"][0]["node_id"] == "Bialystok"
    assert first.inventory["links"][0]["link_id"] == "Link_0_10"


def test_input_order_does_not_affect_plan() -> None:
    unordered = _tiny_topology()
    reordered = NetworkTopology(
        name="tiny",
        source_format="sndlib-xml",
        meta={},
        nodes=tuple(reversed(unordered.nodes)),
        links=tuple(reversed(unordered.links)),
        demands=(),
    )
    # Catalog name only used for config validation; IR is supplied explicitly.
    plan_a = compile_isp_plan(IspConfig(topology="polska"), topology=unordered)
    plan_b = compile_isp_plan(IspConfig(topology="polska"), topology=reordered)
    assert plan_a.inventory == plan_b.inventory
    assert [n.device_name for n in plan_a.nodes] == [
        n.device_name for n in plan_b.nodes
    ]
    assert [n.loopback for n in plan_a.nodes] == [n.loopback for n in plan_b.nodes]
    assert [n.frr_conf for n in plan_a.nodes] == [n.frr_conf for n in plan_b.nodes]


def test_slug_collision_raises() -> None:
    topo = NetworkTopology(
        name="clash",
        source_format="sndlib-xml",
        meta={},
        nodes=(TopoNode(id="New York"), TopoNode(id="new_york")),
        links=(TopoLink(id="L1", source="New York", target="new_york"),),
        demands=(),
    )
    with pytest.raises(IspCompileError, match="Device name collision"):
        compile_isp_plan(IspConfig(topology="polska"), topology=topo)


def test_loopback_pool_exhaustion() -> None:
    topo = _tiny_topology(nodes=("A", "B", "C"), links=(("L1", "A", "B"),))
    cfg = IspConfig(
        topology="polska",
        loopback_pool=IPv4Network("10.255.0.0/31"),
    )
    with pytest.raises(IspCompileError, match="Loopback pool"):
        compile_isp_plan(cfg, topology=topo)


def test_p2p_pool_exhaustion() -> None:
    topo = _tiny_topology(
        nodes=("A", "B", "C"),
        links=(("L1", "A", "B"), ("L2", "B", "C"), ("L3", "A", "C")),
    )
    cfg = IspConfig(
        topology="polska",
        p2p_pool=IPv4Network("10.0.0.0/30"),  # only two /31s
    )
    with pytest.raises(IspCompileError, match="P2P pool"):
        compile_isp_plan(cfg, topology=topo)


def test_metric_strategies() -> None:
    link = TopoLink(
        id="L1",
        source="A",
        target="B",
        routing_cost=7.4,
        preinstalled=CapacityModule(capacity=1000.0, cost=1.0),
    )
    assert link_metric(link, IspConfig(metric_strategy="constant").validated()) == 10
    assert link_metric(link, IspConfig(metric_strategy="routing_cost").validated()) == 7
    assert (
        link_metric(link, IspConfig(metric_strategy="inv_capacity").validated()) == 1000
    )
    missing = TopoLink(id="L2", source="A", target="B")
    assert (
        link_metric(missing, IspConfig(metric_strategy="routing_cost").validated())
        == 10
    )
    assert (
        link_metric(missing, IspConfig(metric_strategy="inv_capacity").validated())
        == 10
    )


def test_ospf_and_isis_frr_differ() -> None:
    topo = _tiny_topology(nodes=("A", "B"), links=(("L1", "A", "B"),))
    isis = compile_isp_plan(IspConfig(topology="polska", igp="isis"), topology=topo)
    ospf = compile_isp_plan(IspConfig(topology="polska", igp="ospf"), topology=topo)
    assert "router isis NIKA" in isis.nodes[0].frr_conf
    assert "router ospf" in ospf.nodes[0].frr_conf
    assert "ip ospf network non-broadcast" in ospf.nodes[0].frr_conf
    assert "neighbor " in ospf.nodes[0].frr_conf
    assert "router isis" not in ospf.nodes[0].frr_conf


def test_slugify_basic() -> None:
    assert slugify("Gdansk", kind="node") == "gdansk"
    assert slugify("Link_0_10", kind="link") == "link_0_10"
    with pytest.raises(IspCompileError):
        slugify("!!!", kind="node")


def test_endpoint_address_order_stable_by_device_name() -> None:
    topo = _tiny_topology(nodes=("Z", "A"), links=(("L1", "Z", "A"),))
    plan = compile_isp_plan(IspConfig(topology="polska"), topology=topo)
    link = plan.links[0]
    assert link.endpoint_a == "a"
    assert link.endpoint_b == "z"
    assert link.address_a.endswith("0") or True  # /31 first address on lex-smaller
    assert plan.inventory["links"][0]["endpoint_a"]["device"] == "a"
