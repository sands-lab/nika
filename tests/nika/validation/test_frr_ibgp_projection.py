from __future__ import annotations

from pathlib import Path

import pytest

from nika.net_env.isp.bgp.frr import merge_frr_conf, render_bgp_frr_fragment
from nika.problems.rca.inventory import load_offline_net_env
from nika.validation.batfish.frr_ibgp import project_frr_ibgp, split_frr_ibgp


@pytest.fixture
def janos_ibgp_configs():
    env = load_offline_net_env(
        "isp_janos-us", igp="isis", bgp_mode="ibgp_rr", backend="kathara"
    )
    bgp_nodes = {node.device_name: node for node in env.bgp_plan.nodes}
    configs = {
        node.device_name: merge_frr_conf(
            node.frr_conf,
            render_bgp_frr_fragment(bgp_nodes[node.device_name], env.bgp_plan),
        )
        for node in env.plan.nodes
    }
    return env, configs


@pytest.mark.unit
def test_real_ibgp_rr_projection_preserves_sessions_origins_and_policy(
    janos_ibgp_configs,
):
    env, configs = janos_ibgp_configs
    rr_plan = next(node for node in env.bgp_plan.nodes if node.device_name == "albany")
    rr = project_frr_ibgp(split_frr_ibgp(configs["albany"])[1])

    assert f"router bgp {rr_plan.asn}" in rr
    assert f"bgp router-id {rr_plan.router_id}" in rr
    assert f"bgp cluster-id {rr_plan.cluster_id}" in rr
    assert "update-source Loopback0" in rr
    assert "route-reflector-client" in rr
    assert "route-policy BGP-IN in" in rr
    assert "route-policy BGP-OUT out" in rr
    assert "prefix-set BUSINESS" in rr
    assert "203.0.113.0/24" in rr

    origin = next(item for item in env.bgp_plan.originated if item.device == "stlouis")
    origin_projection = project_frr_ibgp(split_frr_ibgp(configs["stlouis"])[1])
    assert f"ipv4 address {origin.ping_address}/24" in origin_projection
    assert f"network {origin.prefix}" in origin_projection


@pytest.mark.unit
def test_projection_preserves_removed_route_reflector_client_flag(
    janos_ibgp_configs,
):
    _, configs = janos_ibgp_configs
    fragment = split_frr_ibgp(configs["albany"])[1]
    rr_client = next(
        line.split()[1]
        for line in fragment.splitlines()
        if line.strip().endswith("route-reflector-client")
    )
    mutated = fragment.replace(f" neighbor {rr_client} route-reflector-client\n", "", 1)

    projected = project_frr_ibgp(mutated)

    assert f"neighbor {rr_client}\n" in projected
    assert f"neighbor {rr_client}\n  remote-as 65000" in projected
    assert (
        f"neighbor {rr_client}\n  remote-as 65000\n  update-source Loopback0\n  address-family ipv4 unicast\n   route-reflector-client"
        not in projected
    )


@pytest.mark.unit
def test_projection_preserves_withdrawn_business_network(janos_ibgp_configs):
    _, configs = janos_ibgp_configs
    fragment = split_frr_ibgp(configs["stlouis"])[1]
    mutated = fragment.replace("  network 203.0.113.0/24\n", "", 1)

    projected = project_frr_ibgp(mutated)

    assert "network 203.0.113.0/24" not in projected
    assert "ipv4 address 203.0.113.1/24" in projected


@pytest.mark.unit
def test_projection_preserves_changed_prefix_list_filter(janos_ibgp_configs):
    _, configs = janos_ibgp_configs
    fragment = split_frr_ibgp(configs["albany"])[1]
    mutated = fragment.replace(
        "ip prefix-list BUSINESS seq 5 permit 203.0.113.0/24 le 24",
        "ip prefix-list BUSINESS seq 5 permit 192.0.2.0/24 le 24",
        1,
    )

    projected = project_frr_ibgp(mutated)

    assert "prefix-set BUSINESS\n 192.0.2.0/24" in projected
    assert "prefix-set BUSINESS\n 203.0.113.0/24" not in projected


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace(
            "neighbor 10.255.0.2 remote-as 65000",
            "neighbor 10.255.0.2 password secret\n neighbor 10.255.0.2 remote-as 65000",
            1,
        ),
        lambda text: text.replace(
            "match ip address prefix-list BUSINESS",
            "match ip address prefix-list MISSING",
            1,
        ),
        lambda text: text.replace("route-map BGP-IN in", "route-map MISSING in", 1),
    ],
)
@pytest.mark.unit
def test_projection_rejects_unsupported_attributes_and_undefined_policy(
    janos_ibgp_configs, mutation
):
    _, configs = janos_ibgp_configs
    fragment = split_frr_ibgp(configs["albany"])[1]

    with pytest.raises(ValueError):
        project_frr_ibgp(mutation(fragment))


@pytest.mark.unit
def test_projection_rejects_non_connected_network_origin(janos_ibgp_configs):
    _, configs = janos_ibgp_configs
    fragment = split_frr_ibgp(configs["stlouis"])[1]
    mutated = fragment.replace("network 203.0.113.0/24", "network 203.0.116.0/24", 1)

    with pytest.raises(ValueError, match="non-connected origin"):
        project_frr_ibgp(mutated)


@pytest.mark.contract
def test_selected_isis_ibgp_benchmark_failures_preserve_original_design():
    from nika.net_env.isp.igp.plan import active_igp_links
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.net_env.synthesis.parameters import IsisMetric
    from nika.net_env.synthesis.requirements import resolve_routing_requirements
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.workflows.validation.benchmark_routing import (
        benchmark_isp_profiles,
        build_benchmark_isp_experiment,
    )

    root = Path(__file__).resolve().parents[3]
    profiles = benchmark_isp_profiles(
        [
            root / "benchmark/working/cases.yaml",
            root / "benchmark/releases/0.2.0/dev.yaml",
            root / "benchmark/releases/0.2.0/test.yaml",
        ]
    )
    selected_problems = {
        "bgp_asn_misconfig",
        "bgp_acl_block",
        "bgp_missing_route_advertisement",
        "bgp_hijacking",
    }
    selected = {
        key: profile
        for key, profile in profiles.items()
        if profile["supported"]
        and profile["deployment"]["igp"] == "isis"
        and profile["deployment"]["bgp_mode"] == "ibgp_rr"
        and profile["deployment"]["backend"] == "kathara"
        and any(
            case["problem"] in selected_problems for case in profile["benchmark_cases"]
        )
    }
    assert set(selected) == {
        "isp_cost266--isis--ibgp_rr--kathara",
        "isp_nobel-eu--isis--ibgp_rr--kathara",
        "isp_pioro40--isis--ibgp_rr--kathara",
        "isp_janos-us--isis--ibgp_rr--kathara",
        "isp_zib54--isis--ibgp_rr--kathara",
    }

    for profile in selected.values():
        spec = build_benchmark_isp_experiment(profile)
        deployment = profile["deployment"]
        original = load_offline_net_env(profile["scenario"], **deployment)
        interfaces = {
            (node, interface)
            for link in active_igp_links(original.plan)
            for node, interface in (
                (link.endpoint_a, link.iface_a),
                (link.endpoint_b, link.iface_b),
            )
        }
        metric_bound = get_net_env_instance(
            profile["scenario"],
            **deployment,
            routing_parameters={
                "isis_metrics": [
                    IsisMetric(node=node, interface=interface, metric=1)
                    for node, interface in sorted(interfaces)
                ]
            },
        )

        assert spec.deployment.model_dump(exclude_none=True) == deployment
        assert (metric_bound.igp, metric_bound.bgp_mode, metric_bound.backend) == (
            "isis",
            "ibgp_rr",
            "kathara",
        )
        assert metric_bound.bgp_plan == original.bgp_plan
        assert metric_bound.bgp_plan.originated == original.bgp_plan.originated
        assert all(link.metric == 1 for link in active_igp_links(metric_bound.plan))

        resolved = resolve_routing_requirements(spec, metric_bound)
        healthy_ids = {intent.id for intent in resolved.healthy.intents}
        assert {intent.id for intent in spec.healthy.intents} <= healthy_ids

        role_intent = next(
            intent
            for intent in resolved.healthy.intents
            if intent.adjacency and intent.adjacency.route_reflector_client
        )
        role_static = next(
            mutation
            for mutation in spec.counterexamples
            if mutation.name == "missing-rr-client-role"
        )
        role_runtime = next(
            mutation
            for mutation in spec.runtime_counterexamples
            if mutation.name == "rr-client-role-withdrawal"
        )
        assert role_static.expected_failed_ids == (role_intent.id,)
        assert role_runtime.expected_failed_ids == (role_intent.id,)

        selected_cases = [
            case
            for case in profile["benchmark_cases"]
            if case["problem"] in selected_problems
        ]
        assert selected_cases
        for case in selected_cases:
            failure = next(
                check
                for check in spec.failure_checks
                if check.problem == case["problem"]
            )
            assert failure.params == case["inject"]
            assert set(failure.expected_failed_ids) <= healthy_ids
            if case["problem"] == "bgp_acl_block":
                target = case["inject"]["host_name"]
                assert failure.restore_nft_nodes == (target,)
                behavior_paths = [
                    path
                    for requirement in spec.behavior
                    for path in (
                        *requirement.paths,
                        *(
                            path
                            for paths in requirement.state_paths.values()
                            for path in paths
                        ),
                    )
                ]
                assert all(
                    any(
                        (path[0], path[-1])
                        in {
                            (session.local_device, session.remote_device),
                            (session.remote_device, session.local_device),
                        }
                        for path in behavior_paths
                    )
                    for session in metric_bound.bgp_plan.sessions
                    if session.session_type == "ibgp"
                )
                healthy_adjacencies = {
                    intent.id: intent.adjacency
                    for intent in resolved.healthy.intents
                    if intent.adjacency and intent.adjacency.protocol == "bgp"
                }
                assert any(
                    adjacency is not None
                    and target not in (adjacency.local_node, adjacency.remote_node)
                    for identifier in failure.expected_failed_ids
                    if (adjacency := healthy_adjacencies.get(identifier))
                )
            if case["problem"] == "bgp_hijacking":
                prefix = case["inject"]["target_network"]
                original_isolations = [
                    intent
                    for intent in resolved.healthy.intents
                    if intent.id in failure.expected_failed_ids
                    and intent.property == "isolation"
                    and intent.destination.address == prefix
                ]
                assert original_isolations
                restored_by_id = {
                    intent.id: intent for intent in failure.requirements.intents
                }
                assert all(
                    restored_by_id[intent.id].property == "reachability"
                    and restored_by_id[intent.id].expected == "reachable"
                    for intent in original_isolations
                )
                if case["inject"]["symptom_host"] == case["inject"]["host_name"]:
                    observer = min(
                        origin.device
                        for origin in metric_bound.bgp_plan.originated
                        if origin.device != case["inject"]["host_name"]
                    )
                    observer_id = (
                        f"isolate.unauthorized.{observer}.{prefix.replace('/', '-')}"
                    )
                    assert observer_id in healthy_ids
                    assert restored_by_id[observer_id].property == "reachability"
                    assert restored_by_id[observer_id].expected == "reachable"
                cleanup = failure.recovery_commands[case["inject"]["host_name"]]
                assert any("ip route del local" in command for command in cleanup)
                assert any("ip addr del" in command for command in cleanup)
