"""ISP deploy-option selection and benchmark YAML normalization."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nika.workflows.benchmark.isp_options import (
    isp_column_suffix,
    isp_config_for_problem,
)
from nika.workflows.benchmark.load_config import normalize_benchmark_row
from nika.workflows.benchmark.resume import benchmark_row_fingerprint

_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
if str(_BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARK_DIR))

from generate_benchmark import isp_config_for_problem as gen_isp_config  # noqa: E402


def test_isp_config_rpki() -> None:
    cfg = isp_config_for_problem("bgp_rpki_invalid_route_leak", {"bgp", "rpki"})
    assert cfg == {"topo": "abilene", "igp": "isis", "bgp_mode": "ebgp"}
    assert gen_isp_config("bgp_rpki_invalid_route_leak", {"bgp", "rpki"}) == cfg


def test_isp_config_ospf_and_bgp() -> None:
    assert isp_config_for_problem("ospf_neighbor_missing", {"ospf"}) == {
        "topo": "polska",
        "igp": "ospf",
        "bgp_mode": "none",
    }
    assert isp_config_for_problem("bgp_asn_misconfig", {"bgp"}) == {
        "topo": "polska",
        "igp": "isis",
        "bgp_mode": "ibgp_rr",
    }
    assert isp_config_for_problem("link_down", {"link"}) == {
        "topo": "polska",
        "igp": "isis",
        "bgp_mode": "none",
    }


def test_isp_column_suffix() -> None:
    assert isp_column_suffix(topo="polska", igp="isis", bgp_mode="none") == "isis"
    assert isp_column_suffix(topo="polska", igp="ospf", bgp_mode="none") == "ospf"
    assert isp_column_suffix(topo="polska", igp="isis", bgp_mode="ibgp_rr") == "ibgp_rr"
    assert (
        isp_column_suffix(topo="abilene", igp="isis", bgp_mode="ebgp") == "abilene-ebgp"
    )


def test_normalize_isp_row_fills_and_keeps_options() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "isp",
            "problem": "bgp_rpki_invalid_route_leak",
            "topo_size": None,
            "inject": {"host_name": "losang"},
        }
    )
    assert row["topo"] == "abilene"
    assert row["igp"] == "isis"
    assert row["bgp_mode"] == "ebgp"


def test_normalize_rejects_isp_options_on_non_isp() -> None:
    with pytest.raises(ValueError, match="does not accept topo/igp/bgp_mode"):
        normalize_benchmark_row(
            {
                "scenario": "dc_clos",
                "problem": "link_down",
                "topo_size": "s",
                "workload": "host",
                "bgp_mode": "ibgp_rr",
                "inject": {"host_name": "pc_0_0", "intf_name": "eth0"},
            }
        )


def test_fingerprint_includes_isp_options() -> None:
    base = {
        "scenario": "isp",
        "problem": "bgp_rpki_invalid_route_leak",
        "topo_size": "",
        "inject": {"host_name": "losang"},
        "topo": "abilene",
        "igp": "isis",
        "bgp_mode": "ebgp",
    }
    other = dict(base)
    other["bgp_mode"] = "ibgp_rr"
    assert benchmark_row_fingerprint(base) != benchmark_row_fingerprint(other)
