"""Docker e2e: isp-compatible failures inject + verify_fault.

Full applicable-mode matrix over representative topos (polska, geant).
"""

from __future__ import annotations

import pytest

from nika.net_env.isp.inject_targets import isp_inject_params
from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

REPR_TOPOS = ("polska", "geant")
ALL_IGPS = ("isis", "ospf")
ALL_BGP_MODES = ("none", "ibgp_rr", "ebgp")
BGP_ON_MODES = ("ibgp_rr", "ebgp")

LINK_ICMP_FRR = (
    "link_down",
    "link_flap",
    "link_detach",
    "mtu_mismatch",
    "link_bandwidth_throttling",
    "link_packet_corruption",
    "icmp_acl_block",
    "frr_service_down",
)
OSPF_PROBLEMS = (
    "ospf_area_misconfiguration",
    "ospf_neighbor_missing",
    "ospf_acl_block",
)
BGP_PROBLEMS = (
    "bgp_asn_misconfig",
    "bgp_acl_block",
    "bgp_missing_route_advertisement",
    "bgp_blackhole_route_leak",
    "host_static_blackhole",
    "bgp_hijacking",
)


def _cases_link_icmp_frr() -> list[tuple[str, str, str, str]]:
    return [
        (topo, igp, bgp_mode, problem)
        for topo in REPR_TOPOS
        for igp in ALL_IGPS
        for bgp_mode in ALL_BGP_MODES
        for problem in LINK_ICMP_FRR
    ]


def _cases_ospf() -> list[tuple[str, str, str, str]]:
    return [
        (topo, "ospf", bgp_mode, problem)
        for topo in REPR_TOPOS
        for bgp_mode in ALL_BGP_MODES
        for problem in OSPF_PROBLEMS
    ]


def _cases_bgp() -> list[tuple[str, str, str, str]]:
    return [
        (topo, igp, bgp_mode, problem)
        for topo in REPR_TOPOS
        for igp in ALL_IGPS
        for bgp_mode in BGP_ON_MODES
        for problem in BGP_PROBLEMS
    ]


ALL_CASES = _cases_link_icmp_frr() + _cases_ospf() + _cases_bgp()


def _case_id(topo: str, igp: str, bgp_mode: str, problem: str) -> str:
    return f"{topo}-{igp}-{bgp_mode}-{problem}"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class IspFailureInjectDockerTest(IntegrationTestCase):
    @pytest.mark.parametrize(
        "topo,igp,bgp_mode,problem",
        ALL_CASES,
        ids=[_case_id(*c) for c in ALL_CASES],
    )
    def test_inject_verifies(
        self, topo: str, igp: str, bgp_mode: str, problem: str
    ) -> None:
        isp_plan = compile_isp_plan(
            IspConfig(topology=topo, igp=igp)  # type: ignore[arg-type]
        )
        bgp_inv = None
        if bgp_mode != "none":
            bgp = compile_bgp_plan(isp_plan, bgp_mode)  # type: ignore[arg-type]
            assert bgp is not None
            bgp_inv = bgp.inventory
        params = isp_inject_params(problem, isp_plan.inventory, bgp_inv)

        env_args = ["--topo", topo, "--igp", igp, "--bgp-mode", bgp_mode]
        session_id = self._start_env("isp", env_args)
        try:
            self._assert_session_ready(session_id, "isp")
            self._inject_failure(problem, params, session_id=session_id)
            self._assert_failure_injected(problem, session_id=session_id)
        finally:
            self._close_session(session_id)
