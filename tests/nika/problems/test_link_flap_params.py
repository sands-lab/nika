"""Inject param alignment for link_flap across scenarios."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params

TOPO_SIZE = "s"

FABRIC_SCENARIOS = ("sdn_l3_clos", "p4_dc_fabric")
CORE_SCENARIOS = (
    "simple_bgp",
    "dc_clos",
    "campus_lan",
    "enterprise_branch",
    "p4_dc_gateway",
    "min3clos",
)


@pytest.mark.parametrize("seed", (0, 1, 4, 7))
@pytest.mark.parametrize("scenario", FABRIC_SCENARIOS)
def test_fabric_inject_aligns_probe_path(scenario: str, seed: int) -> None:
    params = resolve_inject_params("link_flap", scenario, TOPO_SIZE, seed=seed)
    assert params["down_time"] == "1"
    assert params["up_time"] == "1"
    assert params.get("probe_dst_ip")
    assert params.get("observer_device", "").startswith("client_")
    assert params["intf_name"] == "eth0"
    assert params["host_name"].startswith("client_")


@pytest.mark.parametrize("seed", (0, 1, 4, 7))
@pytest.mark.parametrize("scenario", CORE_SCENARIOS)
def test_core_inject_timing_and_probe_fields(scenario: str, seed: int) -> None:
    topo = "" if scenario in {"simple_bgp", "min3clos"} else TOPO_SIZE
    params = resolve_inject_params("link_flap", scenario, topo, seed=seed)
    assert params["down_time"] == "1"
    assert params["up_time"] == "1"
    assert params.get("host_name")

    if scenario == "p4_dc_gateway":
        assert params.get("probe_dst_ip", "").startswith("10.")
        assert params["probe_dst_ip"] != "20.0.0.1"
        assert params["host_name"] == "client_1"
        assert params["intf_name"] == "eth0"
    elif scenario == "min3clos":
        assert params["host_name"] == "leaf1"
        assert params["intf_name"] == "e1-1"
        assert params["probe_dst_ip"] == "10.0.0.27"
        assert params["observer_device"] == "client1"
    elif scenario == "dc_clos":
        assert params["host_name"] == "client_0"
        assert params["probe_dst_ip"] == "10.0.1.2"
    elif scenario == "enterprise_branch":
        assert params["probe_dst_ip"] == "10.0.20.2"
    elif scenario == "simple_bgp":
        assert params["host_name"] == "pc1"


def test_isp_inject_uses_one_second_flap() -> None:
    isp_options = {
        "igp": "ospf",
        "bgp_mode": "ebgp",
        "rpki": False,
    }
    params = resolve_inject_params(
        "link_flap", "isp_abilene", "s", seed=1, isp_options=isp_options
    )
    assert params["down_time"] == "1"
    assert params["up_time"] == "1"
    assert params.get("symptom_host")
    assert params.get("probe_dst_ip")
