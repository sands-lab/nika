"""ISP BGP symptom target resolution."""

from __future__ import annotations

import sys

from nika.config import BENCHMARK_DIR

if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from inject_resolve import resolve_inject_params  # noqa: E402


def test_isp_bgp_symptom_targets_attached() -> None:
    inject = resolve_inject_params(
        "bgp_blackhole_route_leak",
        "isp",
        "",
        seed=42,
        isp_options={
            "topo": "abilene",
            "igp": "ospf",
            "bgp_mode": "ebgp",
            "rpki": False,
        },
    )
    assert inject.get("symptom_host")
    assert inject.get("probe_dst_ip")
    assert inject["host_name"] != inject["symptom_host"]
