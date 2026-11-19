import pytest
from nika.net_env.p4_dc_gateway.l4_lb import L4GatewayState, bucket_for
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.inject_resolve import resolve_inject_params

pytestmark = pytest.mark.unit


def test_gateway_and_nat_failures_register() -> None:
    names = (
        "lb_pending_connection_update_race",
        "icmp_frag_needed_filter_misconfiguration",
        "snat_port_pool_exhaustion",
        "nat_mapping_removed_without_drain",
    )
    assert all(get_problem_class(name) is not None for name in names)


def test_l4_state_is_deterministic_and_capacity_bounded() -> None:
    flow_a = ("192.0.2.10", "20.0.0.1", 20000, 80, 6)
    flow_b = ("192.0.2.10", "20.0.0.1", 20001, 80, 6)
    assert bucket_for(flow_a) == bucket_for(flow_a)
    state = L4GatewayState(capacity=1)
    state.learn(flow_a, "10.0.1.11")
    assert state.learn(flow_b, "10.0.1.12") == flow_a
    assert state.occupancy == 1


def test_safe_update_pins_pending_flow_to_old_version() -> None:
    flow = ("192.0.2.10", "20.0.0.1", 20000, 80, 6)
    state = L4GatewayState()
    state.pending[flow] = state.pool_version
    state.safe_update()
    assert state.pool_version == 2
    assert state.version_for(flow) == 1


def test_nat_benchmark_params_use_routed_branch_aliases() -> None:
    params_s = resolve_inject_params(
        "nat_mapping_removed_without_drain", "enterprise_branch", "s", seed=1
    )
    assert params_s["nat_ip_a"] == "198.18.1.10"
    assert params_s["nat_ip_b"] == "198.18.1.11"
    assert params_s["wan_interface"] == "eth2"

    params_m = resolve_inject_params(
        "nat_mapping_removed_without_drain", "enterprise_branch", "m", seed=1
    )
    assert params_m["wan_interface"] == "eth3"


def test_gateway_benchmark_params_target_the_gateway() -> None:
    params = resolve_inject_params(
        "lb_connection_state_exhaustion", "p4_dc_gateway", "s", seed=1
    )
    assert params == {"host_name": "gateway_1", "max_connections": "4"}


def test_native_frag_filter_expires_previously_learned_host_pmtus(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from nika.problems.forwarding_encapsulation_policy import p4_gateway
    from nika.runtime.spec import NodeRole

    cls = get_problem_class("icmp_frag_needed_filter_misconfiguration")
    problem = cls.__new__(cls)
    commands = []
    problem.runtime = SimpleNamespace(
        exec=lambda host, command: commands.append((host, command))
    )
    problem.net_env = SimpleNamespace(
        machine_identities={
            "pc1": SimpleNamespace(role=NodeRole.HOST, capabilities=()),
            "r1": SimpleNamespace(role=NodeRole.ROUTER, capabilities=()),
        }
    )
    ops = Mock(profile="sonic")
    monkeypatch.setattr(p4_gateway, "native_fault_ops", lambda *_: ops)
    problem.inject_fault(cls.Params(host_name="r1"))
    ops.block_icmp_frag_needed.assert_called_once()
    assert len(commands) == 1 and commands[0][0] == "pc1"
    assert "ip route replace" in commands[0][1]


@pytest.mark.parametrize("occupied", [0, 57, 58, 64])
def test_snat_oracle_counts_full_snapshot_before_output_truncation(occupied):
    from nika.mcp.device_read import conntrack_display, MAX_OUTPUT
    from nika.mcp.tool_output import truncate_tool_text

    cls = get_problem_class("snat_port_pool_exhaustion")
    problem = cls("enterprise_branch", topo_size="s")
    check = next(
        step
        for step in problem.oracle_playbook(cls.Params(public_ip="198.18.1.10")).steps
        if "Count the translated source ports" in step.purpose
    ).expect

    def row(index, translated):
        return (
            f"tcp 6 123 TIME_WAIT src=10.1.40.2 dst=198.51.100.10 "
            f"sport={10000 + index} dport=80 src=198.51.100.10 "
            f"dst=198.18.1.10 sport=80 dport={translated} [ASSURED] mark=0 use=1"
        )

    rows = [row(i, 50000 + i) for i in range(90)]
    rows += [row(100 + i, 40000 + i) for i in range(occupied)]
    rows += [row(100 + i, 40000 + i) for i in range(occupied)]
    rows += [row(200 + i, 50100 + i) for i in range(90)]
    raw = "\n".join(rows)
    assert len(raw.encode()) < MAX_OUTPUT
    observation = truncate_tool_text(conntrack_display(raw))
    assert "REPLY_DST_PORTS 198.18.1.10 [" in observation
    assert check(observation) is (occupied >= 58)
    assert not check("REPLY_DST_PORTS 198.18.1.10 [40000-40063")
    assert not check("REPLY_DST_PORTS 198.18.1.11 [40000-40063]")


@pytest.mark.parametrize("split_ports", [False, True])
def test_snat_oracle_counts_distinct_ports_on_the_selected_public_address(split_ports):
    from nika.net_env.net_env_pool import get_net_env_instance

    cls = get_problem_class("snat_port_pool_exhaustion")
    problem = cls.__new__(cls)
    problem.net_env = get_net_env_instance(
        "enterprise_branch", topo_size="s", device_profile="routeros"
    )
    params = cls.Params(public_ip="198.18.1.10")
    step = next(
        step
        for step in problem.oracle_playbook(params).steps
        if "Count the translated source ports" in step.purpose
    )

    def rows(address, ports):
        return "\n".join(
            f"{i} protocol=tcp reply-dst-address={address} reply-dst-port={port}"
            if split_ports
            else f"{i} protocol=tcp reply-dst-address={address}:{port}"
            for i, port in enumerate(ports)
        )

    ports = range(params.port_start, params.port_end + 1)
    assert step.expect(rows(params.public_ip, ports))
    assert not step.expect(rows("198.18.1.100", ports))
    assert not step.expect(rows(params.public_ip, [params.port_start] * len(ports)))
    assert not step.expect(rows(params.public_ip, range(20000, 20064)))
