from nika.net_env.p4_dc_gateway.l4_lb import L4GatewayState, bucket_for
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.inject_resolve import resolve_inject_params


def test_gateway_and_nat_failures_register() -> None:
    names = (
        "lb_connection_state_exhaustion",
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
    assert params["host_name"] == "gateway_1"
    assert params["client_host"] == "client_1"
    assert params["vip_url"] == "http://20.0.0.1:80/"
    assert params["backend_dip"] == "10.0.1.11"
    assert params["attacker_device"] == "client_2"
