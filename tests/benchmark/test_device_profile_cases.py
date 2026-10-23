"""Benchmark cases per router device profile (offline, no Docker)."""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from nika.net_env.net_env_pool import deployment_needs_licensed_images
from nika.problems.registry import get_problem_class, list_avail_problem_instances
from nika.problems.support.router_ops import (
    failure_supports_profile,
    profile_fault_operations,
)
from nika.workflows.benchmark.generate import (
    build_failure_cases,
    iter_failure_case_specs,
    native_benchmark_profiles,
)
from nika.workflows.benchmark.load_config import normalize_benchmark_row

pytestmark = pytest.mark.contract


def test_compatibility_follows_the_vendor_fault_operations() -> None:
    # A failure needs every operation its native path uses; profiles offer
    # the operations their provider implements.
    assert {"bgp_asn", "set_bgp_asn"} <= profile_fault_operations("iosxr")
    asn = get_problem_class("bgp_asn_misconfig")
    assert failure_supports_profile(asn, "iosxr")
    assert failure_supports_profile(asn, "routeros") == (
        {"bgp_asn", "set_bgp_asn"} <= profile_fault_operations("routeros")
    )
    # Acting on the container interface needs no router operation.
    assert failure_supports_profile(get_problem_class("link_down"), "routeros")
    # Without a native implementation a failure stays on FRR/Linux nodes.
    frr_down = get_problem_class("frr_service_down")
    assert not failure_supports_profile(frr_down, "iosxr")


def test_generation_expands_native_profiles_only_at_size_s() -> None:
    assert native_benchmark_profiles("dc_clos", "s") == ["iosxr", "routeros"]
    assert native_benchmark_profiles("dc_clos", "m") == []
    assert native_benchmark_profiles("campus_lan", "l") == []
    assert native_benchmark_profiles("isp_abilene") == ["iosxr", "routeros"]
    assert native_benchmark_profiles("isp_geant") == []  # topology tier m
    assert native_benchmark_profiles("isp_abilene_ebgp_rpki") == []
    specs = {
        (scenario, size, (options or {}).get("device_profile"))
        for problem, scenario, size, options in iter_failure_case_specs()
        if problem == "link_down" and scenario == "dc_clos"
    }
    assert specs == {
        ("dc_clos", "s", None),
        ("dc_clos", "s", "iosxr"),
        ("dc_clos", "s", "routeros"),
        ("dc_clos", "m", None),
        ("dc_clos", "l", None),
    }


def test_targets_avoid_native_routers_that_cannot_run_the_failure() -> None:
    # dc_clos converts every router: frr_service_down has no XR target.
    cases, rejected = build_failure_cases(
        problem="frr_service_down",
        scenario="dc_clos",
        topo_size="s",
        isp_options={"device_profile": "iosxr"},
    )
    assert not cases
    assert {row["reason"] for row in rejected} == {"unsupported_device_profile"}
    # campus_lan converts only its core: OSPF faults keep the FRR routers.
    cases, _ = build_failure_cases(
        problem="ospf_neighbor_missing",
        scenario="campus_lan",
        topo_size="s",
        isp_options={"device_profile": "iosxr"},
    )
    targets = {case["inject"]["host_name"] for case in cases}
    assert targets and not any(name.startswith("router_core") for name in targets)
    # A native implementation targets the XR routers, named as NIKA nodes.
    cases, _ = build_failure_cases(
        problem="bgp_asn_misconfig",
        scenario="dc_clos",
        topo_size="s",
        isp_options={"device_profile": "iosxr"},
    )
    assert cases and all(case["device_profile"] == "iosxr" for case in cases)
    assert cases[0]["root_causes"] == [
        {
            "resource": {"kind": "node", "node": cases[0]["inject"]["host_name"]},
            "fault_type": "bgp_asn_misconfig",
        }
    ]


def test_native_rows_parse_and_need_licensed_images() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos",
            "problem": "link_down",
            "topo_size": "s",
            "device_profile": "routeros",
            "inject": {"host_name": "leaf_router_0_0", "intf_name": "eth0"},
        }
    )
    assert row["device_profile"] == "routeros"
    with pytest.raises(ValueError):
        normalize_benchmark_row(
            {
                "scenario": "sdn_l3_clos",
                "problem": "link_down",
                "topo_size": "s",
                "device_profile": "iosxr",
                "inject": {"host_name": "h1", "intf_name": "eth0"},
            }
        )
    assert deployment_needs_licensed_images("dc_clos", "iosxr")
    assert not deployment_needs_licensed_images("dc_clos")
    assert deployment_needs_licensed_images("routeros_simple_bgp")


def _native_operations_used(problem_cls) -> set[str]:
    """Operations a failure class requests through the native dispatch."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(problem_cls)))
    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if getattr(func, "id", None) == "native_fault_ops" and len(node.args) >= 3:
            used.add(node.args[2].value)
        if isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "ops":
            used.add(func.attr)
    return used


def test_failures_declare_every_native_operation_they_use() -> None:
    for name, problem_cls in list_avail_problem_instances().items():
        used = _native_operations_used(problem_cls)
        declared = problem_cls.native_router_operations
        if used:
            assert declared is not None and used <= declared, (name, used, declared)
