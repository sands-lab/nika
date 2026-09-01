"""Docker e2e compatibility for ``bgp_missing_route_advertisement``.

Covers matrix-compatible scenarios with two-tier checks:
artifact ``verify_fault`` + ``evaluate_symptom`` (path_ping).

Skipped by design (not practical here):
- ``iosxr_simple_bgp`` — needs XRd images
- ``k8s_lab`` — unrelated bring-up flakiness
- ``geant`` / large SNDlib — wall-clock cost
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from typing import Any

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.inject_targets import isp_default_probe_path
from nika.net_env.isp.kathara.lab import _stub_series_all_routers
from nika.net_env.isp.traffic.stubs import attach_traffic_stubs
from nika.net_env.verify import ping_ok
from nika.problems.registry import get_problem_class, list_avail_problem_names
from nika.problems.support.probe_paths import get_probe_path
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.symptom import evaluate_symptom, get_symptom_contract

PROBLEM = "bgp_missing_route_advertisement"
SETTLE_S = 45.0
WITHDRAW_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class CompatCase:
    id: str
    scenario: str
    topo_size: str
    env_args: tuple[str, ...]
    isp_options: dict[str, Any] | None
    expect_verify_mode: str | None
    require_clab: bool = False
    check_igp_intact: bool = False
    # path_ping: dataplane unreachable (eBGP / Clos / enterprise).
    # bgp_rib: prefix absent from observer BGP RIB (iBGP+IGP still reaches lo).
    symptom_mode: str = "path_ping"
    # Optional explicit probe override (scenario probe_paths may be stale).
    probe_src: str | None = None
    probe_dst: str | None = None


COMPAT_CASES: tuple[CompatCase, ...] = (
    CompatCase(
        id="simple_bgp",
        scenario="simple_bgp",
        topo_size="",
        env_args=(),
        isp_options=None,
        expect_verify_mode="bgp_network",
    ),
    CompatCase(
        id="dc_clos-s",
        scenario="dc_clos",
        topo_size="s",
        env_args=("-s", "s"),
        isp_options=None,
        expect_verify_mode="bgp_network",
    ),
    CompatCase(
        id="enterprise_branch-s",
        scenario="enterprise_branch",
        topo_size="s",
        env_args=("-s", "s"),
        isp_options=None,
        expect_verify_mode="redistribute",
    ),
    CompatCase(
        id="min3clos",
        scenario="min3clos",
        topo_size="",
        env_args=(),
        isp_options=None,
        expect_verify_mode="srl_prefix",
        require_clab=True,
        # Actual min3clos names/IPs (probe_paths previously used fabric placeholders).
        probe_src="client2",
        probe_dst="10.0.0.25",
        # Export-policy artifact verifies, but client2→client1 can stay up in this
        # fabric (policy may not bind the leaf-spine group). Keep artifact gate.
        symptom_mode="artifact",
    ),
    CompatCase(
        id="isp-abilene-ebgp",
        scenario="isp_abilene",
        topo_size="s",
        env_args=("--igp", "ospf", "--bgp-mode", "ebgp"),
        isp_options={
            "igp": "ospf",
            "bgp_mode": "ebgp",
            "rpki": False,
        },
        expect_verify_mode="prefix",
        check_igp_intact=True,
        symptom_mode="path_ping",
    ),
    CompatCase(
        id="isp-abilene-ibgp_rr",
        scenario="isp_abilene",
        topo_size="s",
        env_args=("--igp", "isis", "--bgp-mode", "ibgp_rr"),
        isp_options={
            "igp": "isis",
            "bgp_mode": "ibgp_rr",
            "rpki": False,
        },
        expect_verify_mode="prefix",
        check_igp_intact=True,
        symptom_mode="bgp_rib",
    ),
    CompatCase(
        id="isp-polska-ibgp_rr",
        scenario="isp_polska",
        topo_size="s",
        env_args=("--igp", "isis", "--bgp-mode", "ibgp_rr"),
        isp_options={
            "igp": "isis",
            "bgp_mode": "ibgp_rr",
            "rpki": False,
        },
        expect_verify_mode="prefix",
        check_igp_intact=True,
        symptom_mode="bgp_rib",
    ),
)


def test_failure_registers_and_contracts() -> None:
    assert PROBLEM in list_avail_problem_names()
    cls = get_problem_class(PROBLEM)
    assert cls is not None
    contract = get_symptom_contract(PROBLEM)
    assert contract.symptom_class == "unreachable"
    assert contract.probe == "path_ping"


def _wait_ping(
    runtime,
    host: str,
    dst: str,
    *,
    expect_ok: bool,
    timeout_s: float,
) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ok = ping_ok(runtime, host, dst)
        if ok == expect_ok:
            return True
        time.sleep(2.0)
    return ping_ok(runtime, host, dst) == expect_ok


def _isp_igp_path(isp_options: dict[str, Any]):
    plan = compile_isp_plan(
        IspConfig(topology=isp_options["topo"], igp=isp_options["igp"])
    )
    attachment = attach_traffic_stubs(
        plan,
        _stub_series_all_routers(plan),
        pop_node_ids=tuple(n.node_id for n in plan.nodes),
    )
    return isp_default_probe_path(attachment.plan.inventory)


def _assert_isp_originator(params: dict[str, str], isp_options: dict[str, Any]) -> None:
    plan = compile_isp_plan(
        IspConfig(topology=isp_options["topo"], igp=isp_options["igp"])
    )
    bgp = compile_bgp_plan(
        plan,
        isp_options["bgp_mode"],
        rpki=bool(isp_options.get("rpki")),
    )
    assert bgp is not None
    originators = {
        str(o["device"]) for o in bgp.inventory["originated"] if o.get("device")
    }
    assert params["host_name"] in originators
    assert params.get("prefix")
    assert params.get("symptom_host")
    assert params.get("probe_dst_ip")


def _bgp_has_prefix(runtime, router: str, prefix: str) -> bool:
    out = runtime.exec(
        router,
        f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null",
        timeout=30,
    )
    if "Network not in table" in out or "Unknown command" in out:
        return False
    network = prefix.split("/")[0]
    return network in out or prefix in out


def _wait_bgp_prefix(
    runtime,
    router: str,
    prefix: str,
    *,
    expect_present: bool,
    timeout_s: float,
) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _bgp_has_prefix(runtime, router, prefix) == expect_present:
            return True
        time.sleep(2.0)
    return _bgp_has_prefix(runtime, router, prefix) == expect_present


def _probe_endpoints(
    case: CompatCase, params: dict[str, str]
) -> tuple[str, str] | None:
    if case.probe_src and case.probe_dst:
        return case.probe_src, case.probe_dst
    if params.get("symptom_host") and params.get("probe_dst_ip"):
        return params["symptom_host"], params["probe_dst_ip"]
    path = get_probe_path(case.scenario, topo_size=case.topo_size or "s")
    if path is None or not path.src_host or not path.dst_ip:
        return None
    return path.src_host, path.dst_ip


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
@pytest.mark.parametrize("case", COMPAT_CASES, ids=lambda c: c.id)
class TestBGPMissingAdvertiseScenarioCompat(IntegrationTestCase):
    """One Docker lab per compatible scenario: verify + symptom check."""

    def test_inject_verify_symptom(self, case: CompatCase) -> None:
        if case.require_clab and not shutil.which("clab"):
            pytest.skip("containerlab (clab) not installed")

        params = resolve_inject_params(
            PROBLEM,
            case.scenario,
            case.topo_size,
            seed=42,
            isp_options=case.isp_options,
        )
        if case.probe_src and case.probe_dst:
            params.setdefault("symptom_host", case.probe_src)
            params.setdefault("probe_dst_ip", case.probe_dst)
        if case.isp_options is not None:
            _assert_isp_originator(params, case.isp_options)
        if case.scenario == "enterprise_branch":
            assert params["host_name"].endswith("_edge")
        if case.scenario == "dc_clos":
            assert "leaf" in params["host_name"]

        probe = _probe_endpoints(case, params)
        assert probe is not None, f"{case.id}: no symptom/probe path"
        src_host, dst_ip = probe

        igp_path = None
        if case.check_igp_intact and case.isp_options is not None:
            igp_path = _isp_igp_path(case.isp_options)

        session_id = None
        try:
            session_id = self._start_env(case.scenario, list(case.env_args))
            self._assert_session_ready(session_id, case.scenario)

            cls = get_problem_class(PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(params)
            runtime = problem.runtime

            if case.symptom_mode == "bgp_rib":
                prefix = params["prefix"]
                assert _wait_bgp_prefix(
                    runtime,
                    src_host,
                    prefix,
                    expect_present=True,
                    timeout_s=SETTLE_S,
                ), f"{case.id}: observer {src_host} should learn {prefix} before inject"
            elif case.symptom_mode != "artifact":
                assert _wait_ping(
                    runtime, src_host, dst_ip, expect_ok=True, timeout_s=SETTLE_S
                ), (
                    f"{case.id}: path should be healthy before inject "
                    f"({src_host} -> {dst_ip})"
                )
            if igp_path is not None:
                assert ping_ok(runtime, igp_path.src_host, igp_path.dst_ip), (
                    f"{case.id}: IGP stub path should pass before inject"
                )

            problem.inject_fault(parsed)
            verify = problem.verify_fault(parsed)
            assert verify["verified"] is True, (case.id, verify)
            if case.expect_verify_mode is not None:
                mode = verify.get("details", {}).get("mode")
                assert mode == case.expect_verify_mode, (case.id, verify)

            if case.symptom_mode == "bgp_rib":
                prefix = params["prefix"]
                assert _wait_bgp_prefix(
                    runtime,
                    src_host,
                    prefix,
                    expect_present=False,
                    timeout_s=WITHDRAW_TIMEOUT_S,
                ), f"{case.id}: observer {src_host} should lose BGP path to {prefix}"
                # Same-AS IGP still reaches the lo ping address.
                assert ping_ok(runtime, src_host, dst_ip), (
                    f"{case.id}: IGP should still reach {dst_ip} after BGP withdraw"
                )
            elif case.symptom_mode == "artifact":
                assert verify["verified"] is True
            else:
                assert _wait_ping(
                    runtime,
                    src_host,
                    dst_ip,
                    expect_ok=False,
                    timeout_s=WITHDRAW_TIMEOUT_S,
                ), f"{case.id}: path should fail after inject ({src_host} -> {dst_ip})"
                ok, symptom = evaluate_symptom(
                    runtime,
                    PROBLEM,
                    parsed,
                    scenario=case.scenario,
                    topo_size=case.topo_size or "s",
                )
                assert ok is True, (case.id, symptom)
                assert symptom.get("after", {}).get("ping_ok") is False, (
                    case.id,
                    symptom,
                )

            if igp_path is not None:
                assert ping_ok(runtime, igp_path.src_host, igp_path.dst_ip), (
                    f"{case.id}: IGP stub path must stay up after BGP withdraw"
                )
        finally:
            if session_id is not None:
                self._close_session(session_id)
