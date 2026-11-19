"""Inventory-derived route faults retain a control outside their affected prefixes."""

from ipaddress import ip_address, ip_interface, ip_network

import pytest

from nika.net_env.isp.identity import list_isp_base_scenarios
from nika.problems.rca.inventory import (
    iter_link_termination_points,
    load_offline_net_env,
    parse_endpoint,
)
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from scripts.benchmark_audit.live import _independent_control_ip, _resolve_path

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "size,host,intf", [("s", "router_core_2", "eth2"), ("m", "router_dist_1_2", "eth0")]
)
def test_stub_area_control_stays_outside_disconnected_branch(size, host, intf):
    import re
    from types import SimpleNamespace
    from nika.net_env.native_routers import router_sketches
    from nika.problems.routing_control_plane.ospf import ospf_stub_router
    from nika.problems.support.benchmark_targets import hosts_behind_router

    env = load_offline_net_env("campus_lan", size)
    cls = get_problem_class("ospf_stub_area_mismatch")
    problem = cls.__new__(cls)
    problem.net_env = env
    params = cls.Params(host_name=host, intf_name=intf)
    addresses = {}
    for node, (_, startup) in router_sketches(env).items():
        for ip, interface in re.findall(r"ip addr add ([0-9./]+) dev (\S+)", startup):
            addresses[node, interface] = ip
    for node in env.lab.machines:
        startup = (
            env.lab.fs.readtext(f"{node}.startup")
            if env.lab.fs.exists(f"{node}.startup")
            else ""
        )
        for ip, interface in re.findall(r"ip addr add ([0-9./]+) dev (\S+)", startup):
            addresses[node, interface] = ip
    for name, value in [
        ("pc_1_1_1_1", "10.1.1.10/24"),
        ("pc_2_1_1_1", "10.2.1.10/24"),
        ("web_server_3", "10.200.0.3/24"),
    ]:
        addresses[name, "eth0"] = value

    def address(node, interface, with_prefix=False):
        value = addresses.get((node, interface))
        return value if with_prefix or not value else str(ip_interface(value).ip)

    control = _independent_control_ip(
        problem,
        params,
        SimpleNamespace(get_host_ip=address),
        "pc_2_1_1_1",
        "10.200.0.3",
    )
    assert control is not None
    source, destination = control
    affected = hosts_behind_router(env, ospf_stub_router(env, host, intf))
    assert source not in affected
    assert all(
        str(ip_interface(value).ip) != destination
        for (node, _), value in addresses.items()
        if node in affected
    )


class PlanAddresses:
    def __init__(self, env):
        self.addresses = {
            (
                node.device_name,
                interface.name,
            ): f"{interface.address}/{interface.prefixlen}"
            for node in env.plan.nodes
            for interface in node.interfaces
        }
        self.addresses.update(
            {
                (host.host_name, host.host_iface): f"{host.address}/{host.prefixlen}"
                for host in env.traffic.hosts
            }
        )
        if site := env.bgp_plan.customer_site:
            self.addresses[site.host_name, "eth0"] = f"{site.address}/24"
            provider = next(n for n in env.plan.nodes if n.device_name == site.provider)
            self.addresses[site.provider, env.bgp_plan.customer_interface(provider)] = (
                f"{site.gateway}/24"
            )

    def get_host_ip(self, node, interface, with_prefix=False):
        address = self.addresses.get((node, interface))
        return address if with_prefix or not address else str(ip_interface(address).ip)


@pytest.mark.parametrize("fault", ["bgp_blackhole_community_leak", "bgp_hijacking"])
def test_control_avoids_inventory_prefixes_on_every_compatible_graph(fault):
    cls = get_problem_class(fault)
    checked = 0
    for scenario in list_isp_base_scenarios():
        if not cls.is_compatible(scenario, "frr", backend="kathara"):
            continue
        env = load_offline_net_env(scenario, igp="ospf", bgp_mode="ebgp")
        problem = cls.__new__(cls)
        problem.net_env = env
        params = cls.Params(
            **resolve_inject_params(
                fault, scenario, "m", isp_options={"igp": "ospf", "bgp_mode": "ebgp"}
            )
        )
        if fault == "bgp_hijacking":
            params = cls.Params(
                host_name=env.bgp_plan.inventory["leaker_device"],
                variant="rpki_invalid_origin",
            )
        path = _resolve_path(scenario, params, topo_size="m")
        runtime = PlanAddresses(env)
        nets = [ip_network(prefix) for prefix in problem.affected_prefixes(params)]
        # Exercise a faulted destination even when the default RPKI path is healthy.
        destination = str(nets[0].network_address + 1)
        control = _independent_control_ip(
            problem, params, runtime, path.peer_host, destination
        )
        assert control is not None, scenario
        source, target = control
        assert all(ip_address(target) not in net for net in nets), (scenario, control)
        adjacent = {
            runtime.get_host_ip(*parse_endpoint(endpoint))
            for _name, endpoints in iter_link_termination_points(env)
            if source in {parse_endpoint(endpoint)[0] for endpoint in endpoints}
            for endpoint in endpoints
            if parse_endpoint(endpoint)[0] != source
        }
        assert target in adjacent, (scenario, control)
        # Its return address must also remain outside the affected prefixes.
        for (node, _interface), address in runtime.addresses.items():
            if node == source:
                assert all(ip_interface(address).ip not in net for net in nets)
        checked += 1
    assert checked > 0


def test_audit_selection_rejects_unknown_cases_before_starting(monkeypatch):
    from scripts.benchmark_audit import matrix

    row = {"scenario": "campus_lan", "topo_size": "s", "problem": "healthy"}
    monkeypatch.setattr(matrix, "audit_plan", lambda: [row])
    monkeypatch.setattr(matrix, "read_git_commit", lambda: ("commit", False))
    monkeypatch.setattr(
        matrix, "_run_group", lambda *args, **kwargs: pytest.fail("started audit")
    )
    with pytest.raises(ValueError, match="Unknown audit cases"):
        matrix.run_matrix(cases={matrix.case_id(row), "typo"}, force=True)
    with pytest.raises(ValueError, match="repeats must be at least 1"):
        matrix.run_matrix(repeats=0)


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_gateway_control_uses_reachable_data_endpoint_outside_fault(size):
    from types import SimpleNamespace

    from nika.telemetry import apply_telemetry

    env = load_offline_net_env("p4_dc_gateway", size)
    apply_telemetry(env, ("prometheus",))
    cls = get_problem_class("p4_table_entry_misconfig")
    problem = cls.__new__(cls)
    problem.net_env = env
    target = env.model.services[0]
    params = cls.Params(host_name=target.attached_switch, target_ip=target.ip)
    endpoints = {item.name: item for item in env.model.endpoints}

    def address(node, interface, with_prefix=False):
        endpoint = endpoints.get(node)
        ip = endpoint.ip if endpoint and interface == "eth0" else "172.30.0.12"
        return f"{ip}/24" if with_prefix else ip

    source, destination = _independent_control_ip(
        problem, params, SimpleNamespace(get_host_ip=address), "client_2", target.ip
    )
    control = next(item for item in endpoints.values() if item.ip == destination)
    assert control.attached_switch != target.attached_switch
    assert not env.probe_intent().isolation_matcher()(source, control.name)


@pytest.mark.parametrize(
    "faults",
    [
        [{"fault_type": "link_down", "confirmed": False}],
        [
            {"fault_type": "link_down", "confirmed": False},
            {"fault_type": "link_flap", "error": "tool unavailable"},
        ],
    ],
)
def test_negative_controls_require_every_planned_playbook(monkeypatch, faults):
    from pathlib import Path
    from scripts.benchmark_audit import live

    monkeypatch.setattr(
        live,
        "_confusable_plans",
        lambda *args: ({"link_down": {}, "link_flap": {}}, {}),
    )
    monkeypatch.setattr(
        live,
        "_run_oracle",
        lambda *args, **kwargs: (Path("oracle.json"), {"faults": faults}),
    )
    assert (
        live._neighbor_oracle_stage("session", "link_down", None, {}, None).status
        == "no_evidence"
    )


def test_negative_controls_respect_declared_deployment_compatibility():
    from scripts.benchmark_audit.environment import identity_from_row
    from scripts.benchmark_audit.live import _confusable_plans

    env = load_offline_net_env("enterprise_branch", "m")
    identity = identity_from_row(
        {
            "scenario": "enterprise_branch",
            "topo_size": "m",
            "problem": "host_missing_ip",
            "inject": {"host_name": "br1_corp_pc", "intf_name": "eth0"},
        }
    )
    planned, skipped = _confusable_plans(
        "host_missing_ip", identity, {"topo_size": "m"}, env
    )
    assert not get_problem_class("dhcp_service_down").is_compatible("enterprise_branch")
    assert "dhcp_service_down" not in planned and "dhcp_service_down" in skipped
    assert planned["host_incorrect_ip"]["host_name"] == "br1_corp_pc"


def test_device_and_link_controls_inspect_the_same_attachment():
    from scripts.benchmark_audit.environment import identity_from_row
    from scripts.benchmark_audit.live import _confusable_plans

    env = load_offline_net_env("enterprise_branch", "s")
    row = {
        "scenario": "enterprise_branch",
        "topo_size": "s",
        "problem": "device_flow_blackhole",
        "inject": {"forwarding_device": "hq_edge", "intf_name": "eth0", "seed": "42"},
    }
    plans, _ = _confusable_plans(
        row["problem"], identity_from_row(row), {"topo_size": "s"}, env
    )
    assert plans["link_packet_corruption"]["host_name"] == "hq_edge"
    assert plans["link_packet_corruption"]["intf_name"] == "eth0"
    row.update(
        problem="link_packet_corruption",
        inject={"host_name": "hq_corp_pc", "intf_name": "eth0"},
    )
    plans, _ = _confusable_plans(
        row["problem"], identity_from_row(row), {"topo_size": "s"}, env
    )
    for fault in ("device_flow_blackhole", "device_forwarding_packet_corruption"):
        assert plans[fault]["forwarding_device"] == "hq_edge"
        assert plans[fault]["intf_name"] == "eth0"


@pytest.mark.parametrize("failure", ["resolve", "plan", "unexpected_plan"])
@pytest.mark.parametrize("partial", [False, True])
def test_neighbor_planner_errors_fail_audit_instead_of_skipping(
    monkeypatch, failure, partial
):
    from scripts.benchmark_audit import live
    from scripts.benchmark_audit.environment import identity_from_row

    identity = identity_from_row(
        {"scenario": "enterprise_branch", "topo_size": "m", "problem": "link_down"}
    )

    if partial:
        monkeypatch.setattr(
            get_problem_class("link_down"),
            "CONFUSABLE_WITH",
            ("host_incorrect_ip", "link_flap"),
        )

    def broken(fault, *args, **kwargs):
        if fault == "host_incorrect_ip":
            return {}
        if failure == "unexpected_plan":
            raise RuntimeError("planner broke")
        raise ValueError("planner broke")

    monkeypatch.setattr(
        "nika.workflows.benchmark.inject_resolve.resolve_inject_params",
        broken if failure == "resolve" else lambda *a, **kw: {},
    )
    monkeypatch.setattr("nika.agents.oracle.plan.fault_plan", broken)
    stage = live._neighbor_oracle_stage("session", "link_down", identity, {}, None)
    assert stage.status == "fail"
    assert "planner broke" in stage.reason
    assert "traceback" in stage.evidence


def test_neighbor_planner_uses_default_target_when_shared_target_is_invalid(
    monkeypatch,
):
    from scripts.benchmark_audit import live
    from scripts.benchmark_audit.environment import identity_from_row

    identity = identity_from_row(
        {
            "scenario": "enterprise_branch",
            "problem": "link_down",
            "inject": {"host_name": "incompatible"},
        }
    )
    monkeypatch.setattr(
        "nika.workflows.benchmark.inject_resolve.resolve_inject_params",
        lambda *a, **kw: {"host_name": "default"},
    )

    def plan(fault, scenario, scenario_params, inject):
        if inject["host_name"] == "incompatible":
            raise ValueError("invalid target")

    monkeypatch.setattr("nika.agents.oracle.plan.fault_plan", plan)
    planned, skipped = live._confusable_plans("link_down", identity, {}, None)
    assert planned
    assert all(params["host_name"] == "default" for params in planned.values())
    assert skipped == {}


@pytest.mark.parametrize("profile", ["routeros", "iosxr"])
def test_neighbor_planning_uses_declared_native_failure_operations(profile):
    from scripts.benchmark_audit.environment import identity_from_row
    from scripts.benchmark_audit.live import _confusable_plans

    env = load_offline_net_env("dc_clos", "s", device_profile=profile)
    row = {
        "scenario": "dc_clos",
        "topo_size": "s",
        "device_profile": profile,
        "problem": "bgp_acl_block",
        "inject": {"host_name": "spine_1"},
    }
    planned, skipped = _confusable_plans(
        row["problem"],
        identity_from_row(row),
        {"topo_size": "s", "device_profile": profile},
        env,
    )
    assert "bgp_md5_password_mismatch" in planned
    if profile == "routeros":
        assert "bgp_ttl_security_mismatch" not in planned
        assert "routeros" in skipped["bgp_ttl_security_mismatch"]
    else:
        assert "bgp_ttl_security_mismatch" in planned
        assert "bgp_ttl_security_mismatch" not in skipped


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_native_policy_control_requires_an_eligible_native_ebgp_target(profile):
    from nika.problems.support.benchmark_targets import NoValidTargetError
    from nika.problems.support.router_ops import native_router_profile
    from scripts.benchmark_audit.environment import identity_from_row
    from scripts.benchmark_audit.live import _confusable_plans

    params = {"device_profile": profile, "backend": "kathara", "topo_size": "s"}
    env = load_offline_net_env("enterprise_branch", "s", device_profile=profile)
    row = {
        "scenario": "enterprise_branch",
        "topo_size": "s",
        "device_profile": profile,
        "backend": "kathara",
        "problem": "bgp_prefix_list_length_mismatch",
        "inject": {"host_name": "br2_edge"},
    }
    planned, skipped = _confusable_plans(
        row["problem"], identity_from_row(row), params, env
    )
    assert "bgp_missing_route_advertisement" in planned
    policy = "bgp_session_policy_missing"
    if profile == "iosxr":
        assert policy not in planned
        assert "native eBGP router" in skipped[policy]
        with pytest.raises(NoValidTargetError, match="native eBGP router"):
            resolve_inject_params(
                policy, "enterprise_branch", "s", isp_options=params, net_env=env
            )
    else:
        assert policy in planned and policy not in skipped
        assert native_router_profile(env, planned[policy]["host_name"]) == profile


def test_neighbor_stage_keeps_declared_incompatibility_unsupported(monkeypatch):
    from scripts.benchmark_audit import live
    from scripts.benchmark_audit.environment import identity_from_row

    monkeypatch.setattr(
        get_problem_class("host_missing_ip"), "CONFUSABLE_WITH", ("dhcp_service_down",)
    )
    identity = identity_from_row(
        {"scenario": "enterprise_branch", "problem": "host_missing_ip"}
    )
    stage = live._neighbor_oracle_stage(
        "session", "host_missing_ip", identity, {}, None
    )
    assert stage.status == "unsupported"
    assert stage.reason == "no_confusable_on_scenario"
    assert stage.evidence["planned"] == {}
    assert stage.evidence["skipped"] == {
        "dhcp_service_down": "Failure is not supported on this deployment."
    }


def test_case_image_selection_includes_healthy_tasks(monkeypatch):
    from scripts.benchmark_audit import matrix

    healthy = {"scenario": "campus_lan", "topo_size": "s", "problem": "healthy"}
    fault = {"scenario": "campus_lan", "topo_size": "s", "problem": "link_down"}
    monkeypatch.setenv(matrix.CASE_IMAGES_ENV, "1")
    monkeypatch.setattr(matrix, "_release_rows", lambda: [healthy, fault, healthy])
    monkeypatch.setattr(matrix, "read_git_commit", lambda: ("commit", False))
    monkeypatch.setattr(matrix, "result_current", lambda *args: False)
    queued = []
    monkeypatch.setattr(
        matrix, "_run_group", lambda rows, **kwargs: queued.extend(rows)
    )
    selected = {matrix.case_id(healthy), matrix.case_id(fault)}
    assert not matrix.run_matrix(cases=selected)
    assert {matrix.case_id(row) for row, _ in queued} == selected
    assert len(queued) == 2


def test_dns_lookalikes_use_declared_servers_instead_of_the_faulted_client():
    from scripts.benchmark_audit.environment import identity_from_row
    from scripts.benchmark_audit.live import _confusable_plans

    env = load_offline_net_env("dc_clos", "s")
    dns_servers = set(env.servers["dns"])
    client = next(host for host in env.hosts if host not in dns_servers)
    row = {
        "scenario": "dc_clos",
        "topo_size": "s",
        "problem": "host_incorrect_dns",
        "inject": {"host_name": client},
    }
    planned, _ = _confusable_plans(
        row["problem"], identity_from_row(row), {"topo_size": "s"}, env
    )
    for fault in ("dns_record_error", "dns_service_down"):
        assert planned[fault]["host_name"] in dns_servers
        assert planned[fault]["host_name"] != client


def test_audit_rejects_mixed_script_and_runtime_checkouts(monkeypatch, tmp_path):
    from scripts.benchmark_audit import matrix

    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(matrix, "read_git_commit", lambda: ("commit", False))
    monkeypatch.setattr(
        matrix, "audit_plan", lambda: pytest.fail("read cases with mixed source roots")
    )
    with pytest.raises(SystemExit, match="different checkouts"):
        matrix.run_matrix()


@pytest.mark.parametrize(
    "fault,control",
    [
        ("bgp_asn_misconfig", "bgp_acl_block"),
        ("bgp_missing_route_advertisement", "bgp_asn_misconfig"),
    ],
)
def test_ibgp_route_reflector_faults_keep_supported_neighbor_controls(fault, control):
    from scripts.benchmark_audit.environment import identity_from_row
    from scripts.benchmark_audit.live import _confusable_plans

    params = {
        "igp": "isis",
        "bgp_mode": "ibgp_rr",
        "device_profile": "frr",
        "backend": "kathara",
    }
    env = load_offline_net_env("isp_sun", "m", **params)
    row = {
        "scenario": "isp_sun",
        "topo_size": "m",
        "problem": fault,
        "inject": {"host_name": "n1"},
        **params,
    }
    planned, skipped = _confusable_plans(fault, identity_from_row(row), params, env)
    assert planned[control]["host_name"] == "n1"
    assert control not in skipped


@pytest.mark.parametrize(
    "fault,control,role",
    [
        ("switch_datapath_id_conflict", "southbound_port_mismatch", "controller"),
        ("southbound_port_mismatch", "switch_datapath_id_conflict", "switch"),
    ],
)
def test_sdn_neighbor_controls_use_their_declared_target_role(fault, control, role):
    from scripts.benchmark_audit.environment import identity_from_row
    from scripts.benchmark_audit.live import _confusable_plans
    from nika.workflows.benchmark.inject_resolve import validate_benchmark_case

    env = load_offline_net_env("sdn_l3_clos", "m")
    inject = resolve_inject_params(fault, "sdn_l3_clos", "m", net_env=env)
    row = {
        "scenario": "sdn_l3_clos",
        "topo_size": "m",
        "problem": fault,
        "inject": inject,
    }
    planned, skipped = _confusable_plans(
        fault, identity_from_row(row), {"topo_size": "m"}, env
    )
    target = planned[control]["host_name"]
    assert env.machine_identities[target].role.value == role
    assert target != inject["host_name"]
    assert control not in skipped
    with pytest.raises(ValueError, match="declared"):
        validate_benchmark_case(
            "sdn_l3_clos",
            control,
            {**planned[control], "host_name": inject["host_name"]},
            "m",
            net_env=env,
            combined=True,
        )


@pytest.mark.parametrize("scenario,size", [("isp_cost266", "m"), ("dc_clos", "s")])
def test_restored_session_environment_preserves_topology_identity(scenario, size):
    from types import SimpleNamespace
    from scripts.benchmark_audit.live import _session_net_env

    params = {"topo_size": size, "lab_name": "restored-copy"}
    env = _session_net_env(
        SimpleNamespace(
            scenario_name=scenario, scenario_params=params, lab_name="restored-copy"
        )
    )
    assert env.lab.machines
    assert env.lab.name == "restored-copy"
    if scenario == "dc_clos":
        assert env.topo_size == size
    assert params == {"topo_size": size, "lab_name": "restored-copy"}


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("scenario", ["dc_clos", "isp_polska"])
def test_session_environment_retains_deployed_containerlab_paths(
    tmp_path, nested, scenario
):
    from types import SimpleNamespace
    from scripts.benchmark_audit.live import _session_net_env

    topology = tmp_path / "deployed.clab.yaml"
    topology.write_text("name: deployed-copy\ntopology:\n  nodes: {}\n")
    params = {"topo_size": "s", "device_profile": "sonic"}
    paths = {"topology_file": str(topology), "runtime_workdir": str(tmp_path)}
    session = SimpleNamespace(
        scenario_name=scenario,
        scenario_params={**params, **paths} if nested else params,
        lab_name="deployed-copy",
        backend="containerlab",
        **({} if nested else paths),
    )

    env = _session_net_env(session)
    runtime = env._build_runtime()

    assert runtime.backend == "containerlab"
    assert runtime.lab_name == "deployed-copy"
    assert runtime.topology_file == topology
    assert runtime.runtime_workdir == tmp_path


def test_session_environment_rejects_missing_containerlab_topology():
    from types import SimpleNamespace
    from scripts.benchmark_audit.live import _session_net_env

    env = _session_net_env(
        SimpleNamespace(
            scenario_name="dc_clos",
            scenario_params={"topo_size": "s", "device_profile": "sonic"},
            lab_name="missing-topology",
            backend="containerlab",
        )
    )
    with pytest.raises(ValueError, match="has no topology_file"):
        env._build_runtime()
