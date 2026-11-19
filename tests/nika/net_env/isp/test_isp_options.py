"""ISP deploy-option selection and benchmark YAML normalization."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.load_config import normalize_benchmark_row
from nika.workflows.benchmark.resume import benchmark_row_fingerprint

pytestmark = pytest.mark.contract


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
                "problem": "bgp_hijacking",
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
        "problem": "bgp_acl_block",
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
