"""Parametrized failure inject + ground-truth contract across backends.

Verify-only path: inject → ``verify_fault`` → ground truth. Does not call
``evaluate_symptom``.
"""

from __future__ import annotations

import os
import platform
import shutil

import pytest

from nika.mcp.session_context import SESSION_ID_ENV
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.support.ci_depth import artifact_verify_only
from tests.support.failure_contract import (
    inject_and_assert_ground_truth,
    resolve_inject_params,
)
from tests.support.prerequisites import docker_available, linux_vrf_available

pytestmark = [pytest.mark.integration, pytest.mark.nightly]

HOST = "pc1"
INTF = "eth0"
LINK_PARAMS = {"host_name": HOST, "intf_name": INTF}

SIMPLE_BGP_FAILURES = (
    "link_down",
    "link_flap",
    "link_down",
    "link_capacity_bottleneck",
    "host_missing_ip",
    "host_incorrect_gateway",
    "bgp_missing_route_advertisement",
    "host_incorrect_ip",
)

CAMPUS_LAN_FAILURES = (
    "link_flap",
    "link_packet_corruption",
    "link_capacity_bottleneck",
    "host_incorrect_dns",
    "ospf_interface_not_enabled",
    "dhcp_service_down",
    "dns_port_blocked",
    "mtu_mismatch",
)

ENTERPRISE_BRANCH_LINK_FAILURES = (
    "link_flap",
    "link_packet_corruption",
    "link_capacity_bottleneck",
)

P4_GATEWAY_LINK_FAILURES = (
    "link_flap",
    "link_packet_corruption",
    "link_capacity_bottleneck",
)

CLAB_CLOS_FAILURES = (
    "link_down",
    "link_down",
    "link_flap",
    "bgp_acl_block",
    "bgp_asn_misconfig",
    "bgp_missing_route_advertisement",
    "host_static_blackhole",
)


def _kathara_cases():
    for problem in SIMPLE_BGP_FAILURES:
        if problem in {"link_down", "link_flap", "link_down"}:
            params = dict(LINK_PARAMS)
        elif problem == "link_capacity_bottleneck":
            params = {
                **LINK_PARAMS,
                "rate": "200kbit",
                "burst": "64kb",
                "limit": "500kb",
            }
        elif problem == "host_missing_ip":
            params = {"host_name": HOST, "intf_name": INTF}
        elif problem == "host_incorrect_gateway" or problem == "host_incorrect_ip":
            params = {"host_name": HOST}
        else:
            params = None
        yield pytest.param(
            "simple_bgp",
            [],
            problem,
            params,
            id=f"kathara-simple_bgp-{problem}",
        )
    for problem in CAMPUS_LAN_FAILURES:
        yield pytest.param(
            "campus_lan",
            ["-s", "s"],
            problem,
            None,
            id=f"kathara-campus_lan-{problem}",
        )
    for problem in ENTERPRISE_BRANCH_LINK_FAILURES:
        yield pytest.param(
            "enterprise_branch",
            ["-s", "s"],
            problem,
            None,
            id=f"kathara-enterprise_branch-{problem}",
        )
    for problem in P4_GATEWAY_LINK_FAILURES:
        yield pytest.param(
            "p4_dc_gateway",
            ["-s", "s"],
            problem,
            None,
            id=f"kathara-p4_dc_gateway-{problem}",
        )


def _clab_cases():
    for profile in ("nokia_srlinux", "sonic"):
        problems = (
            tuple(
                p for p in CLAB_CLOS_FAILURES if p not in {"link_down", "bgp_acl_block"}
            )
            if profile == "nokia_srlinux"
            else ("link_down", "bgp_acl_block")
        )
        for problem in problems:
            yield pytest.param(
                "dc_clos",
                ["-s", "s", "--backend", "containerlab", "--device-profile", profile],
                problem,
                None,
                id=f"clab-dc_clos-{profile}-{problem}",
                marks=pytest.mark.skipif(
                    profile == "sonic"
                    and platform.machine().lower() not in {"x86_64", "amd64"},
                    reason="docker-sonic-vs requires amd64",
                ),
            )


@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario,env_run_args,problem,inject_params",
    list(_kathara_cases()),
)
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
def test_kathara_failure_inject_contract(
    scenario: str,
    env_run_args: list[str],
    problem: str,
    inject_params: dict[str, str] | None,
) -> None:
    if scenario == "enterprise_branch" and not linux_vrf_available():
        pytest.skip("Host kernel lacks Linux VRF (required by enterprise_branch)")
    if artifact_verify_only() and problem in {
        "link_down",
        "link_packet_corruption",
    }:
        pytest.skip(f"{problem} is unreliable on shared artifact-depth runners")
    from tests.support.integration_base import _parse_env_run_args

    options = _parse_env_run_args(env_run_args)
    topo_size = options.pop("topo_size", None)
    if inject_params is None:
        inject_params = resolve_inject_params(
            scenario,
            problem,
            topo_size=topo_size or "",
            isp_options=options,
        )
    session_id = start_net_env(
        scenario,
        topo_size,
        session_tag=resolve_session_tag(context="test"),
        **options,
    )
    prev = os.environ.get(SESSION_ID_ENV)
    os.environ[SESSION_ID_ENV] = session_id
    try:
        inject_and_assert_ground_truth(session_id, scenario, problem, inject_params)
    finally:
        close_session(session_id=session_id)
        if prev is None:
            os.environ.pop(SESSION_ID_ENV, None)
        else:
            os.environ[SESSION_ID_ENV] = prev


@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario,env_run_args,problem,inject_params",
    list(_clab_cases()),
)
@pytest.mark.skipif(not shutil.which("clab"), reason="containerlab not installed")
def test_clab_failure_inject_contract(
    scenario: str,
    env_run_args: list[str],
    problem: str,
    inject_params: dict[str, str] | None,
) -> None:
    from tests.support.integration_base import _parse_env_run_args

    options = _parse_env_run_args(env_run_args)
    topo_size = options.pop("topo_size", "s")
    if inject_params is None:
        inject_params = resolve_inject_params(
            scenario,
            problem,
            topo_size=topo_size,
            isp_options=options,
        )
    session_id = start_net_env(
        scenario,
        topo_size,
        session_tag=resolve_session_tag(context="test"),
        **options,
    )
    prev = os.environ.get(SESSION_ID_ENV)
    os.environ[SESSION_ID_ENV] = session_id
    try:
        inject_and_assert_ground_truth(session_id, scenario, problem, inject_params)
    finally:
        close_session(session_id=session_id)
        if prev is None:
            os.environ.pop(SESSION_ID_ENV, None)
        else:
            os.environ[SESSION_ID_ENV] = prev
