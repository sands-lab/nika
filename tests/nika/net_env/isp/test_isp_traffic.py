"""Unit tests for SNDlib traffic IR, cache, stubs, and OD mapping (no Docker)."""

from __future__ import annotations

from pathlib import Path

from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.traffic import (
    attach_traffic_stubs,
    dynamic_cache_dir,
    resolve_traffic_series,
    series_from_demands,
    series_to_od_dicts,
    stub_host_name,
    write_normalized_series,
)
from nika.net_env.isp.traffic.models import (
    TrafficFlow,
    TrafficInterval,
    TrafficMatrixSeries,
)
from nika.topology import load_sndlib_topology


def test_demands_series_polska() -> None:
    topo = load_sndlib_topology("polska")
    series = series_from_demands(topo, duration_sec=5)
    assert series.source == "demands"
    assert len(series.intervals) == 1
    assert series.intervals[0].flows
    assert len(series.active_node_ids()) >= 2


def test_resolve_none() -> None:
    assert resolve_traffic_series("polska", "none") is None


def test_dynamic_missing_falls_back_to_demands(tmp_path: Path) -> None:
    series = resolve_traffic_series(
        "polska", "dynamic", cache_root=tmp_path / "empty_cache"
    )
    assert series is not None
    assert series.source == "demands"


def test_cache_round_trip(tmp_path: Path) -> None:
    series = TrafficMatrixSeries(
        topology="abilene",
        source="dynamic",
        intervals=(
            TrafficInterval(
                index=0,
                duration_sec=300,
                flows=(
                    TrafficFlow("ATLAM5", "ATLAng", 10.0),
                    TrafficFlow("ATLAng", "ATLAM5", 5.0),
                ),
            ),
            TrafficInterval(
                index=1,
                duration_sec=300,
                flows=(TrafficFlow("ATLAM5", "ATLAng", 20.0),),
            ),
        ),
        sample_period_sec=300,
    )
    cache_dir = dynamic_cache_dir("abilene", cache_root=tmp_path)
    write_normalized_series(series, cache_dir)
    loaded = resolve_traffic_series("abilene", "dynamic", cache_root=tmp_path)
    assert loaded is not None
    assert loaded.source == "dynamic"
    assert len(loaded.intervals) == 2
    assert loaded.intervals[0].flows[0].rate == 10.0


def test_attach_stubs_and_od_mapping() -> None:
    plan = compile_isp_plan(IspConfig(topology="polska"))
    series = resolve_traffic_series("polska", "demands")
    assert series is not None
    attachment = attach_traffic_stubs(plan, series, scale=2.0)
    assert attachment.hosts
    assert all(h.host_name.startswith("pc_") for h in attachment.hosts)
    assert stub_host_name("warsaw") == "pc_warsaw"
    assert any(i.passive for n in attachment.plan.nodes for i in n.interfaces)
    assert attachment.edge_links[0].prefixlen == 30
    od_list = series_to_od_dicts(series, scale=2.0, inventory=attachment.plan.inventory)
    assert len(od_list) == 1
    assert od_list[0]
    src = next(iter(od_list[0]))
    assert src.startswith("pc_")
    dst = next(iter(od_list[0][src]))
    assert dst.startswith("pc_")
    assert od_list[0][src][dst] >= 1


def test_frr_passive_edge_no_ospf_neighbor() -> None:
    plan = compile_isp_plan(IspConfig(topology="polska", igp="ospf"))
    series = resolve_traffic_series("polska", "demands")
    assert series is not None
    attachment = attach_traffic_stubs(plan, series)
    node = next(
        n for n in attachment.plan.nodes if any(i.passive for i in n.interfaces)
    )
    passive = next(i for i in node.interfaces if i.passive)
    assert f"passive-interface {passive.name}" in node.frr_conf
    assert f"neighbor {passive.peer_address}" not in node.frr_conf
