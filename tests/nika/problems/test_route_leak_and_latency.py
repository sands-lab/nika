"""Origin identity and external-link latency regression contracts."""

import json

import pytest

from nika.problems.link_interface.latency import delay_configured
from nika.problems.routing_control_plane.route_leak import (
    BGPRouteLeak,
    learned_origin_path,
)
from nika.problems.registry import get_problem_class
from nika.net_env.net_env_pool import list_all_net_envs


@pytest.mark.unit
def test_route_leak_requires_preserved_origin_and_valid_best_path():
    def route(path, state="valid", best=True):
        return json.dumps(
            {
                "paths": [
                    {
                        "aspath": {"string": path},
                        "rpkiValidationState": state,
                        "bestpath": {"overall": best},
                    }
                ]
            }
        )

    def leaked(raw):
        return learned_origin_path(
            raw, leaker_asn=65002, origin_asn=65001, require_valid=True
        )

    assert leaked(route("65002 65001"))
    assert not leaked(route("65002"))
    assert not leaked(route("65002 65001", state="invalid"))
    assert not leaked(route("65002 65001", best=False))
    assert not leaked('{"paths": []}')
    assert not leaked("tool_error")


@pytest.mark.unit
@pytest.mark.parametrize("value", ["100ms", "100000us", "0.1s"])
def test_latency_verifies_the_requested_external_delay(value):
    assert delay_configured(f"qdisc netem 1: root delay {value}", 100)
    assert not delay_configured(f"qdisc netem 1: root delay {value}", 200)
    assert not delay_configured("qdisc noqueue 0: root", 100)


@pytest.mark.contract
def test_route_leak_requires_a_qualified_frr_ebgp_graph():
    assert BGPRouteLeak.is_compatible("isp_abilene/ebgp")
    assert not BGPRouteLeak.is_compatible("isp_atlanta/ebgp")
    assert not BGPRouteLeak.is_compatible("isp_abilene/ibgp_rr")
    assert not BGPRouteLeak.is_compatible("dc_clos")
    assert not BGPRouteLeak.is_compatible("isp_abilene/ebgp", backend="containerlab")
    assert not BGPRouteLeak.is_compatible("isp_abilene/ebgp", "routeros")


@pytest.mark.contract
def test_healthy_export_guard_preserves_security_and_customer_preference():
    from nika.net_env.isp.bgp import compile_bgp_plan
    from nika.net_env.isp.bgp.frr import render_bgp_frr_fragment
    from nika.net_env.isp.bgp.rpki_profile import slurm_document
    from nika.net_env.isp.igp import IspConfig, compile_isp_plan

    plan = compile_bgp_plan(
        compile_isp_plan(IspConfig(topology="abilene")), "ebgp", rpki=True, rtbh=True
    )
    roles = plan.inventory["inter_as_roles"]
    assert {row["role"] for row in roles} == {"provider", "customer", "peer"}
    for node in plan.nodes:
        config = render_bgp_frr_fragment(node, plan)
        if any(
            row["node"] == node.device_name and row["role"] == "provider"
            for row in roles
        ):
            assert "bgp as-path access-list LOCAL-ORIGIN permit ^$" in config
            assert "match as-path LOCAL-ORIGIN" in config
        if any(
            row["node"] == node.device_name and row["role"] == "customer"
            for row in roles
        ):
            assert "set local-preference 200" in config
    assertions = slurm_document()["locallyAddedAssertions"]["prefixAssertions"]
    assert any(
        row["prefix"] == "198.51.100.0/24" and row["asn"] == 65001 for row in assertions
    )


@pytest.mark.unit
def test_recovery_requires_the_injecting_instances_snapshot():
    for failure in ("bgp_route_leak", "link_latency"):
        cls = get_problem_class(failure)
        problem = cls.__new__(cls)
        assert problem.recover_fault(cls.Params(host_name="r1"))["verified"] is False


@pytest.mark.contract
def test_latency_has_the_same_deployment_compatibility_as_link_corruption():
    latency = get_problem_class("link_latency")
    corruption = get_problem_class("link_packet_corruption")
    for scenario in list_all_net_envs():
        for profile in (None, "frr", "sonic", "nokia_srlinux", "iosxr", "routeros"):
            for backend in ("kathara", "containerlab"):
                assert latency.is_compatible(
                    scenario, profile, backend=backend
                ) == corruption.is_compatible(scenario, profile, backend=backend)


@pytest.mark.contract
@pytest.mark.parametrize(
    "backend,profile",
    [
        ("kathara", "frr"),
        ("kathara", "iosxr"),
        ("containerlab", "nokia_srlinux"),
        ("containerlab", "sonic"),
    ],
)
def test_latency_target_uses_the_backends_catalog_interface(backend, profile):
    from nika.workflows.benchmark.inject_resolve import (
        resolve_inject_params,
        validate_benchmark_case,
    )

    options = {"backend": backend, "device_profile": profile}
    inject = resolve_inject_params(
        "link_latency", "dc_clos", "s", seed=1, isp_options=options
    )
    validate_benchmark_case("dc_clos", "link_latency", inject, "s", isp_options=options)


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["dc_clos", "isp_abilene"])
def test_latency_oracle_runs_active_probes_on_endpoints(scenario):
    from nika.agents.oracle.plan import fault_plan
    from nika.mcp.gateway.access import SHELL_NODE_ROLES
    from nika.problems.registry import get_problem_instance
    from nika.workflows.benchmark.inject_resolve import resolve_inject_params

    inject = resolve_inject_params("link_latency", scenario, "s", seed=1)
    plan = fault_plan("link_latency", scenario, {"topo_size": "s"}, inject)
    env = get_problem_instance(["link_latency"], scenario, topo_size="s").net_env
    for step in plan.playbook.steps:
        if step.tool == "exec_shell" and isinstance(step.args, dict):
            assert (
                env.machine_identities[step.args["host_name"]].role.value
                in SHELL_NODE_ROLES
            )


@pytest.mark.contract
def test_latency_oracle_rejects_capacity_queueing_without_tcp_window_dependency():
    from nika.agents.oracle.plan import fault_plan

    plan = fault_plan(
        "link_latency",
        "enterprise_branch",
        {"topo_size": "s"},
        {"host_name": "hq_srv", "intf_name": "eth0"},
    )
    step = next(step for step in plan.playbook.steps if step.tool == "iperf_test")
    assert "-u" in step.args["client_args"].split()
    assert step.args["client_host_name"] == "hq_srv"
    assert step.expect("[  5] 0.00-10.00 sec 5.9 MBytes 4950 Kbits/sec 0/4000 receiver")
    assert not step.expect(
        "[  5] 0.00-10.00 sec 1.1 MBytes 920 Kbits/sec 3100/4000 receiver"
    )
    assert not step.expect("iperf3: error - unable to connect to server")
