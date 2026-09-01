"""ISP BGP symptom target resolution."""

from __future__ import annotations

from nika.workflows.benchmark.inject_resolve import resolve_inject_params


def test_isp_bgp_symptom_targets_attached() -> None:
    inject = resolve_inject_params(
        "bgp_blackhole_community_leak",
        "isp_abilene_ebgp_rtbh",
        "",
        seed=42,
    )
    assert inject.get("symptom_host")
    assert inject.get("probe_dst_ip")
    assert inject["host_name"] != inject["symptom_host"]
    assert inject["probe_dst_ip"] == "198.51.100.1"
