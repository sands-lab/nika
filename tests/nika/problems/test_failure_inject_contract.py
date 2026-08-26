"""Parametrized failure inject + ground-truth contract across backends.

Verify-only path: inject → ``verify_fault`` → ground truth. Does not call
``evaluate_symptom``.
"""

from __future__ import annotations

import os
import shutil

import pytest

from nika.service.mcp_server.mcp_session_context import SESSION_ID_ENV
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from nika.utils.session_id import resolve_session_tag
from tests.support.failure_contract import (
    inject_and_assert_ground_truth,
    resolve_inject_params,
)
from tests.support.prerequisites import docker_available

HOST = "pc1"
INTF = "eth0"
LINK_PARAMS = {"host_name": HOST, "intf_name": INTF}

SIMPLE_BGP_FAILURES = (
    "link_down",
    "link_flap",
    "link_detach",
    "link_capacity_bottleneck",
    "host_missing_ip",
    "host_incorrect_gateway",
    "bgp_missing_route_advertisement",
    "host_incorrect_ip",
)

CAMPUS_LAN_FAILURES = (
    "host_incorrect_dns",
    "ospf_neighbor_missing",
    "dhcp_service_down",
    "dns_port_blocked",
    "mtu_mismatch",
)

MIN3CLOS_FAILURES = (
    "link_down",
    "link_detach",
    "link_flap",
    "link_capacity_bottleneck",
    "link_packet_corruption",
    "bgp_acl_block",
    "bgp_asn_misconfig",
    "bgp_missing_route_advertisement",
    "host_static_blackhole",
    "bgp_blackhole_route_leak",
    "bgp_hijacking",
)


def _kathara_cases():
    for problem in SIMPLE_BGP_FAILURES:
        if problem in {"link_down", "link_flap", "link_detach"}:
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
        elif problem == "host_incorrect_gateway":
            params = {"host_name": HOST}
        elif problem == "host_incorrect_ip":
            params = {"host_name": HOST}
        else:
            params = resolve_inject_params("simple_bgp", problem)
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
            resolve_inject_params("campus_lan", problem, topo_size="s"),
            id=f"kathara-campus_lan-{problem}",
        )


def _clab_cases():
    for problem in MIN3CLOS_FAILURES:
        yield pytest.param(
            "min3clos",
            [],
            problem,
            resolve_inject_params("min3clos", problem),
            id=f"clab-min3clos-{problem}",
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
    inject_params: dict[str, str],
) -> None:
    topo_size = None
    if "-s" in env_run_args:
        topo_size = env_run_args[env_run_args.index("-s") + 1]
    session_id = start_net_env(
        scenario,
        topo_size,
        session_tag=resolve_session_tag(context="test"),
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
    inject_params: dict[str, str],
) -> None:
    session_id = start_net_env(
        scenario,
        None,
        session_tag=resolve_session_tag(context="test"),
        backend="containerlab",
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
