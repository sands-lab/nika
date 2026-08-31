"""Docker e2e: named ISP RTBH community leaks (Kathara + FRR)."""

from __future__ import annotations

import time

import pytest

from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.problems.registry import get_problem_class
from nika.service.kathara import KatharaFRRAPI
from nika.service.kathara.base_api import KatharaBaseAPI
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

PROBLEM = "bgp_blackhole_community_leak"
ENV_ARGS: list[str] = []


def _rtbh_roles(topology: str) -> dict[str, str]:
    isp_plan = compile_isp_plan(IspConfig(topology=topology, igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp", rtbh=True)
    assert bgp is not None and bgp.inventory.get("rtbh")
    inv = bgp.inventory
    return {
        "leaker": str(inv["leaker_device"]),
        "origin": str(inv["legitimate_origin_device"]),
        "provider": str(inv["rtbh_provider_device"]),
        "prefix": str(inv["target_prefix"]),
        "community": str(inv["blackhole_community"]),
        "ping_addr": str(inv["target_ping_address"]),
        "observer": str(inv["data_plane_observer_host"]),
        "discard_nh": str(inv["discard_next_hop"]),
        "origin_asn": str(inv["legitimate_origin_asn"]),
    }


def _inject_params(topology: str) -> dict[str, str]:
    return {"host_name": _rtbh_roles(topology)["leaker"]}


def _ping_ok(base: KatharaBaseAPI, host: str, dst: str) -> bool:
    out = base.exec_cmd(host, f"ping -c 3 -W 2 {dst} 2>&1", timeout=20)
    return (
        "3 received" in out or "3 packets received" in out or " 0% packet loss" in out
    )


def _ping_fail(base: KatharaBaseAPI, host: str, dst: str) -> bool:
    out = base.exec_cmd(host, f"ping -c 3 -W 2 {dst} 2>&1", timeout=20)
    return (
        "0 received" in out
        or "100% packet loss" in out
        or "unreachable" in out.lower()
        or not _ping_ok(base, host, dst)
    )


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestBGPBlackholeCommunityLeakE2E(IntegrationTestCase):
    """Exercise both release contexts with detailed symptom checks."""

    @pytest.mark.parametrize(
        ("scenario", "topology"),
        (
            ("isp_abilene_ebgp_rtbh", "abilene"),
            ("isp_dfn-bwin_ebgp_rtbh", "dfn-bwin"),
        ),
    )
    def test_rtbh_community_leak_cycle(self, scenario: str, topology: str) -> None:
        roles = _rtbh_roles(topology)
        params = _inject_params(topology)

        session_id = self._start_env(scenario, ENV_ARGS)
        try:
            self._assert_session_ready(session_id, scenario)
            row = self._session_row(session_id)
            lab_name = row["lab_name"]

            time.sleep(30)
            base = KatharaBaseAPI(lab_name=lab_name)
            frr = KatharaFRRAPI(lab_name=lab_name)

            origin_cfg = frr.frr_get_bgp_conf(roles["origin"])
            assert roles["prefix"] in origin_cfg
            assert _ping_ok(base, roles["observer"], roles["ping_addr"])

            provider_bgp = frr.frr_get_routing_state(
                roles["provider"], prefix=roles["prefix"]
            )
            assert roles["community"] not in provider_bgp

            self._inject_failure(PROBLEM, params, session_id=session_id)
            self._assert_failure_injected(PROBLEM, session_id=session_id)
            time.sleep(10)

            leaker_bgp = frr.frr_get_routing_state(
                roles["leaker"], prefix=roles["prefix"]
            )
            assert roles["origin_asn"] in leaker_bgp
            provider_bgp = frr.frr_get_routing_state(
                roles["provider"], prefix=roles["prefix"]
            )
            assert roles["community"] in provider_bgp
            provider_fib = base.exec_cmd(
                roles["provider"],
                f"vtysh -c 'show ip route {roles['prefix']}' 2>/dev/null || true",
            )
            assert roles["discard_nh"] in provider_fib or "Null0" in provider_fib
            assert _ping_fail(base, roles["observer"], roles["ping_addr"])

            for device in (roles["leaker"], roles["origin"], roles["provider"]):
                summary = frr.frr_get_routing_state(device)
                assert summary.strip()

            problem = self._problem(get_problem_class(PROBLEM), session_id=session_id)
            parsed = problem.parse_params(params)
            recovered = problem.recover_fault(parsed)
            assert recovered.get("verified"), recovered
            time.sleep(10)

            provider_bgp = frr.frr_get_routing_state(
                roles["provider"], prefix=roles["prefix"]
            )
            assert roles["community"] not in provider_bgp
            assert _ping_ok(base, roles["observer"], roles["ping_addr"])
        finally:
            self._close_session(session_id)
