"""OSPF enterprise workload selection for benchmark generation."""

from __future__ import annotations

import sys
from pathlib import Path

_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
if str(_BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARK_DIR))

from generate_benchmark import workload_for_campus_lan  # noqa: E402


def test_static_overrides_for_host_ip_faults() -> None:
    assert workload_for_campus_lan("host_incorrect_ip", {"pc", "icmp"}) == "static"
    assert workload_for_campus_lan("host_incorrect_netmask", {"pc", "icmp"}) == "static"
    assert workload_for_campus_lan("host_missing_ip", {"pc", "icmp"}) == "static"


def test_dhcp_for_service_and_shared_faults() -> None:
    assert workload_for_campus_lan("dhcp_service_down", {"dhcp"}) == "dhcp"
    assert workload_for_campus_lan("dns_service_down", {"dns", "http"}) == "dhcp"
    assert (
        workload_for_campus_lan("load_balancer_overload", {"load_balancer", "web"})
        == "dhcp"
    )
    assert workload_for_campus_lan("ospf_neighbor_missing", {"ospf", "frr"}) == "dhcp"
    assert workload_for_campus_lan("link_down", {"link"}) == "dhcp"
