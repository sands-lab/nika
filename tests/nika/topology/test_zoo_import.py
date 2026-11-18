from __future__ import annotations

import pytest

from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.igp.plan import igp_components
from nika.topology import link_preinstalled_capacity, list_topologies
from nika.topology.catalog import (
    ISP_TOPOLOGY_NAMES,
    SNDLIB_TOPOLOGY_NAMES,
    ZOO_TOPOLOGY_NAMES,
    load_topology,
)
from nika.topology.parse_graphml import parse_zoo_graphml

pytestmark = pytest.mark.unit

_GRAPHML = """<?xml version="1.0" encoding="utf-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key attr.name="Network" attr.type="string" for="graph" id="g0" />
  <key attr.name="label" attr.type="string" for="node" id="n0" />
  <key attr.name="Longitude" attr.type="double" for="node" id="n1" />
  <key attr.name="Latitude" attr.type="double" for="node" id="n2" />
  <key attr.name="LinkSpeedRaw" attr.type="double" for="edge" id="e0" />
  <graph edgedefault="undirected">
    <data key="g0">Example</data>
    <node id="0"><data key="n0">Z\u00fcrich</data>
      <data key="n1">8.55</data><data key="n2">47.37</data></node>
    <node id="1"><data key="n0">New York</data></node>
    <node id="2"><data key="n0">New York</data></node>
    <node id="3"></node>
    <node id="4"><data key="n0">Island</data></node>
    <node id="5"><data key="n0">Island Peer</data></node>
    <node id="6"><data key="n0">WestNet Salt Lake City</data></node>
    <node id="7"><data key="n0">WestNet Salt Lake City</data></node>
    <edge source="0" target="1"><data key="e0">10000000000.0</data></edge>
    <edge source="3" target="6" />
    <edge source="6" target="7" />
    <edge source="0" target="1" />
    <edge source="1" target="2" />
    <edge source="2" target="3" />
    <edge source="3" target="3" />
    <edge source="4" target="5" />
  </graph>
</graphml>
"""


def test_parse_normalizes_zoo_graphml() -> None:
    topo = parse_zoo_graphml(_GRAPHML, name="example")

    assert topo.source_format == "topology-zoo-graphml"
    assert topo.demands == ()
    assert topo.meta["Network"] == "Example"
    assert topo.meta["dropped_nodes"] == "2"
    assert [node.id for node in topo.nodes] == [
        "n3",
        "new_york",
        "new_york_2",
        "westnet_salt_lake_2",
        "westnet_salt_lake_c",
        "zurich",
    ]
    zurich = next(node for node in topo.nodes if node.id == "zurich")
    assert (zurich.x, zurich.y) == (8.55, 47.37)

    endpoints = sorted((link.source, link.target) for link in topo.links)
    assert endpoints == [
        ("n3", "westnet_salt_lake_c"),
        ("new_york", "new_york_2"),
        ("new_york_2", "n3"),
        ("westnet_salt_lake_c", "westnet_salt_lake_2"),
        ("zurich", "new_york"),
        ("zurich", "new_york"),
    ]
    capacities = sorted(
        cap for link in topo.links if (cap := link_preinstalled_capacity(link))
    )
    assert capacities == [10000.0]


def test_catalog_matches_vendored_files_and_is_disjoint_from_sndlib() -> None:
    assert list_topologies() == sorted(ISP_TOPOLOGY_NAMES)
    assert not set(ZOO_TOPOLOGY_NAMES) & set(SNDLIB_TOPOLOGY_NAMES)
    assert ISP_TOPOLOGY_NAMES == SNDLIB_TOPOLOGY_NAMES + ZOO_TOPOLOGY_NAMES


@pytest.mark.parametrize("name", ZOO_TOPOLOGY_NAMES)
def test_each_zoo_topology_compiles_connected_isp(name: str) -> None:
    topo = load_topology(name)
    assert topo.source_format == "topology-zoo-graphml"
    assert all(len(node.id) <= 19 for node in topo.nodes)
    for igp in ("isis", "ospf"):
        plan = compile_isp_plan(IspConfig(topology=name, igp=igp))
        assert len(plan.nodes) == len(topo.nodes)
        assert len(igp_components(plan)) == 1
    assert compile_bgp_plan(plan, "ebgp") is not None
