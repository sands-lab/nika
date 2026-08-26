from __future__ import annotations

import pytest
import shutil
from typing import ClassVar
from tests.support.integration_base import PerTestEnvTestCase

HOST = "leaf1"
INTF = "e1-1"
LINK_PARAMS = {"host_name": HOST, "intf_name": INTF}
MIN3CLOS_FAILURES = (
    "link_down",
    "link_detach",
    "link_flap",
    "mtu_mismatch",
    "link_bandwidth_throttling",
    "link_packet_corruption",
    "bgp_acl_block",
    "bgp_asn_misconfig",
    "bgp_missing_route_advertisement",
    "host_static_blackhole",
    "bgp_blackhole_route_leak",
    "bgp_hijacking",
)


@pytest.mark.skipif(not shutil.which("clab"), reason="containerlab not installed")
class ClabFailureInjectVerifyTest(PerTestEnvTestCase):
    SCENARIO = "min3clos"
    ENV_RUN_ARGS: ClassVar[list[str]] = []

    def _inject_and_assert(self, problem: str) -> None:
        params = self._benchmark_inject_from_yaml(self.SCENARIO, problem)
        self._inject_failure(problem, params)
        self._assert_failure_injected(problem)

    def test_link_down(self) -> None:
        self._inject_and_assert("link_down")

    def test_link_detach(self) -> None:
        self._inject_and_assert("link_detach")

    def test_link_flap(self) -> None:
        self._inject_and_assert("link_flap")

    def test_mtu_mismatch(self) -> None:
        self._inject_and_assert("mtu_mismatch")

    def test_link_bandwidth_throttling(self) -> None:
        self._inject_and_assert("link_bandwidth_throttling")

    def test_link_packet_corruption(self) -> None:
        self._inject_and_assert("link_packet_corruption")

    def test_bgp_acl_block(self) -> None:
        self._inject_and_assert("bgp_acl_block")

    def test_bgp_asn_misconfig(self) -> None:
        self._inject_and_assert("bgp_asn_misconfig")

    def test_bgp_missing_route_advertisement(self) -> None:
        self._inject_and_assert("bgp_missing_route_advertisement")

    def test_host_static_blackhole(self) -> None:
        self._inject_and_assert("host_static_blackhole")

    def test_bgp_blackhole_route_leak(self) -> None:
        self._inject_and_assert("bgp_blackhole_route_leak")

    def test_bgp_hijacking(self) -> None:
        self._inject_and_assert("bgp_hijacking")
