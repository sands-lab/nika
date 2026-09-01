"""ISP deploy-option selection and benchmark YAML normalization."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.isp_options import (
    isp_column_suffix,
    isp_config_for_problem,
)
from nika.workflows.benchmark.load_config import normalize_benchmark_row
from nika.workflows.benchmark.resume import benchmark_row_fingerprint


def test_isp_config_does_not_select_rpki_for_max_prefix() -> None:
    cfg = isp_config_for_problem("bgp_max_prefix_exceeded", {"bgp", "isp"})
    assert cfg == {
        "igp": "ospf",
        "bgp_mode": "ebgp",
        "rpki": False,
    }


def test_isp_config_rpki_default() -> None:
    cfg = isp_config_for_problem("bgp_rpki_invalid_route_leak", {"rpki"})
    assert cfg == {
        "igp": "ospf",
        "bgp_mode": "ebgp",
        "rpki": True,
    }


def test_isp_config_ospf_and_bgp() -> None:
    assert isp_config_for_problem("ospf_neighbor_missing", {"ospf"}) == {
        "igp": "ospf",
        "bgp_mode": "none",
        "rpki": False,
    }
    assert isp_config_for_problem("bgp_asn_misconfig", {"bgp"}) == {
        "igp": "isis",
        "bgp_mode": "ibgp_rr",
        "rpki": False,
    }
    assert isp_config_for_problem("link_down", {"link"}) == {
        "igp": "isis",
        "bgp_mode": "none",
        "rpki": False,
    }


def test_isp_column_suffix() -> None:
    assert isp_column_suffix(topo="polska", igp="isis", bgp_mode="none") == "isis"
    assert isp_column_suffix(topo="polska", igp="ospf", bgp_mode="none") == "ospf"
    assert isp_column_suffix(topo="polska", igp="isis", bgp_mode="ibgp_rr") == "ibgp_rr"
    assert (
        isp_column_suffix(topo="abilene", igp="ospf", bgp_mode="ebgp") == "abilene-ebgp"
    )
    assert (
        isp_column_suffix(topo="abilene", igp="ospf", bgp_mode="ebgp", rpki=True)
        == "abilene-ebgp-rpki"
    )
    assert (
        isp_column_suffix(topo="geant", igp="ospf", bgp_mode="ebgp", rpki=True)
        == "geant-ebgp-rpki"
    )


def test_normalize_isp_rpki_named_scenario() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "isp_abilene_ebgp_rpki",
            "problem": "bgp_rpki_invalid_route_leak",
            "topo_size": None,
            "inject": {"host_name": "kscyng"},
        }
    )
    assert row["scenario"] == "isp_abilene_ebgp_rpki"
    assert row["topo_size"] == "s"
    assert "igp" not in row
    assert "bgp_mode" not in row
    assert "rpki" not in row


def test_normalize_isp_base_infers_fixed_size() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "isp_geant",
            "problem": "link_down",
            "topo_size": None,
            "inject": {"host_name": "router_a", "intf_name": "eth0"},
            "igp": "ospf",
            "bgp_mode": "none",
            "rpki": False,
        }
    )
    assert row["topo_size"] == "m"
    assert row["igp"] == "ospf"
    assert row["bgp_mode"] == "none"
    assert row["rpki"] is False


def test_normalize_rejects_isp_wrong_fixed_size() -> None:
    with pytest.raises(ValueError, match="fixed topo_size"):
        normalize_benchmark_row(
            {
                "scenario": "isp_geant",
                "problem": "link_down",
                "topo_size": "s",
                "inject": {"host_name": "router_a", "intf_name": "eth0"},
                "igp": "ospf",
                "bgp_mode": "none",
                "rpki": False,
            }
        )


def test_normalize_rejects_topo_field_on_isp() -> None:
    with pytest.raises(ValueError, match="omit topo"):
        normalize_benchmark_row(
            {
                "scenario": "isp_abilene",
                "problem": "link_down",
                "topo_size": "s",
                "inject": {"host_name": "router_a", "intf_name": "eth0"},
                "topo": "abilene",
                "igp": "ospf",
                "bgp_mode": "none",
                "rpki": False,
            }
        )


def test_normalize_rejects_retired_ebgp_rpki_scenario() -> None:
    with pytest.raises(ValueError, match="not found in the pool"):
        normalize_benchmark_row(
            {
                "scenario": "ebgp_rpki",
                "problem": "bgp_rpki_invalid_route_leak",
                "topo_size": None,
                "inject": {"host_name": "kscyng"},
            }
        )


def test_normalize_rejects_isp_options_on_non_isp() -> None:
    with pytest.raises(ValueError, match="does not accept topo/igp/bgp_mode"):
        normalize_benchmark_row(
            {
                "scenario": "dc_clos",
                "problem": "link_down",
                "topo_size": "s",
                "bgp_mode": "ibgp_rr",
                "inject": {"host_name": "pc_0_0", "intf_name": "eth0"},
            }
        )


def test_fingerprint_includes_isp_options() -> None:
    base = {
        "scenario": "isp_abilene",
        "problem": "bgp_max_prefix_exceeded",
        "topo_size": "s",
        "inject": {"receiver_name": "nyc", "peer_name": "chicago"},
        "igp": "isis",
        "bgp_mode": "ebgp",
        "rpki": False,
    }
    other = dict(base)
    other["bgp_mode"] = "ibgp_rr"
    assert benchmark_row_fingerprint(base) != benchmark_row_fingerprint(other)
    rpki = dict(base)
    rpki["rpki"] = True
    assert benchmark_row_fingerprint(base) != benchmark_row_fingerprint(rpki)
