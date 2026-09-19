from __future__ import annotations

from pathlib import Path

import pytest

from nika.topology import (
    SndlibParseError,
    SndlibUnsupportedError,
    SndlibValidationError,
    link_preinstalled_capacity,
    list_sndlib_topologies,
    load_sndlib_topology,
)
from nika.topology.sndlib.catalog import SNDLIB_TOPOLOGY_NAMES
from nika.topology.sndlib.parse import parse_sndlib_xml

GOLDEN_COUNTS: dict[str, tuple[int, int, int]] = {
    "abilene": (12, 15, 132),
    "atlanta": (15, 22, 210),
    "brain": (161, 332, 14311),
    "cost266": (37, 57, 1332),
    "dfn-bwin": (10, 45, 90),
    "dfn-gwin": (11, 47, 110),
    "di-yuan": (11, 42, 22),
    "france": (25, 45, 300),
    "geant": (22, 36, 462),
    "germany50": (50, 88, 662),
    "giul39": (39, 172, 1471),
    "india35": (35, 80, 595),
    "janos-us": (26, 84, 650),
    "janos-us-ca": (39, 122, 1482),
    "newyork": (16, 49, 240),
    "nobel-eu": (28, 41, 378),
    "nobel-germany": (17, 26, 121),
    "nobel-us": (14, 21, 91),
    "norway": (27, 51, 702),
    "pdh": (11, 34, 24),
    "pioro40": (40, 89, 780),
    "polska": (12, 18, 66),
    "sun": (27, 102, 67),
    "ta1": (24, 55, 396),
    "ta2": (65, 108, 1869),
    "zib54": (54, 81, 1501),
}


def test_catalog_lists_all_expected_topologies() -> None:
    names = list_sndlib_topologies()
    assert names == sorted(SNDLIB_TOPOLOGY_NAMES)
    assert set(names) == set(GOLDEN_COUNTS)


@pytest.mark.parametrize("name", sorted(GOLDEN_COUNTS))
def test_each_topology_converts(name: str) -> None:
    topo = load_sndlib_topology(name)
    nodes_n, links_n, demands_n = GOLDEN_COUNTS[name]

    assert topo.name == name
    assert topo.source_format == "sndlib-xml"
    assert len(topo.nodes) == nodes_n
    assert len(topo.links) == links_n
    assert len(topo.demands) == demands_n

    node_ids = [node.id for node in topo.nodes]
    link_ids = [link.id for link in topo.links]
    demand_ids = [demand.id for demand in topo.demands]
    assert node_ids == sorted(node_ids)
    assert link_ids == sorted(link_ids)
    assert demand_ids == sorted(demand_ids)

    node_set = set(node_ids)
    link_set = set(link_ids)
    for link in topo.links:
        assert link.source in node_set
        assert link.target in node_set
        # Capacity helper never invents values from additional modules.
        pre = link_preinstalled_capacity(link)
        if link.preinstalled is None:
            assert pre is None
        else:
            assert pre == link.preinstalled.capacity

    for demand in topo.demands:
        assert demand.source in node_set
        assert demand.target in node_set
        assert demand.demand_value > 0
        for path in demand.admissible_paths:
            assert path.link_ids
            assert all(link_id in link_set for link_id in path.link_ids)


def test_load_is_stable() -> None:
    first = load_sndlib_topology("polska")
    second = load_sndlib_topology("polska")
    assert first == second
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    assert [link.id for link in first.links] == [link.id for link in second.links]


def test_load_from_path() -> None:
    path = (
        Path(__file__).resolve().parents[3]
        / "src/nika/net_env/isp/sndlib/atlanta/network.xml"
    )
    topo = load_sndlib_topology(path)
    assert topo.name == "atlanta"
    assert len(topo.nodes) == 15


def test_reject_native_ascii() -> None:
    native = (
        "?SNDlib native format; type: network; version: 1.0\nNODES (\n Atlanta\n)\n"
    )
    with pytest.raises(SndlibUnsupportedError, match="native ASCII"):
        parse_sndlib_xml(native, name="native-demo")


def test_reject_wrong_root() -> None:
    xml = '<?xml version="1.0"?><solution xmlns="http://sndlib.zib.de/network"/>'
    with pytest.raises(SndlibUnsupportedError, match="unsupported root"):
        parse_sndlib_xml(xml, name="bad-root")


def test_reject_duplicate_node_ids() -> None:
    xml = """<?xml version="1.0"?>
<network xmlns="http://sndlib.zib.de/network" version="1.0">
  <networkStructure>
    <nodes>
      <node id="A"/>
      <node id="A"/>
    </nodes>
    <links>
      <link id="L1"><source>A</source><target>A</target></link>
    </links>
  </networkStructure>
  <demands/>
</network>
"""
    with pytest.raises(SndlibValidationError, match="duplicate node"):
        parse_sndlib_xml(xml, name="dup-nodes")


def test_reject_dangling_link_endpoint() -> None:
    xml = """<?xml version="1.0"?>
<network xmlns="http://sndlib.zib.de/network" version="1.0">
  <networkStructure>
    <nodes>
      <node id="A"/>
    </nodes>
    <links>
      <link id="L1"><source>A</source><target>B</target></link>
    </links>
  </networkStructure>
  <demands/>
</network>
"""
    with pytest.raises(SndlibValidationError, match="unknown target node"):
        parse_sndlib_xml(xml, name="dangling-link")


def test_reject_empty_nodes() -> None:
    xml = """<?xml version="1.0"?>
<network xmlns="http://sndlib.zib.de/network" version="1.0">
  <networkStructure>
    <nodes/>
    <links>
      <link id="L1"><source>A</source><target>B</target></link>
    </links>
  </networkStructure>
  <demands/>
</network>
"""
    with pytest.raises(SndlibValidationError, match="no nodes"):
        parse_sndlib_xml(xml, name="empty-nodes")


def test_reject_unknown_admissible_link() -> None:
    xml = """<?xml version="1.0"?>
<network xmlns="http://sndlib.zib.de/network" version="1.0">
  <networkStructure>
    <nodes>
      <node id="A"/>
      <node id="B"/>
    </nodes>
    <links>
      <link id="L1"><source>A</source><target>B</target></link>
    </links>
  </networkStructure>
  <demands>
    <demand id="D1">
      <source>A</source>
      <target>B</target>
      <demandValue>1.0</demandValue>
      <admissiblePaths>
        <admissiblePath id="P1">
          <linkId>missing</linkId>
        </admissiblePath>
      </admissiblePaths>
    </demand>
  </demands>
</network>
"""
    with pytest.raises(SndlibValidationError, match="unknown linkId"):
        parse_sndlib_xml(xml, name="bad-path")


def test_reject_invalid_xml() -> None:
    with pytest.raises(SndlibParseError, match="invalid XML"):
        parse_sndlib_xml("<network>", name="broken")


def test_unknown_catalog_name() -> None:
    with pytest.raises(SndlibUnsupportedError, match="unknown SNDlib topology"):
        load_sndlib_topology("not-a-real-topo")
