"""Benchmark IS-IS iBGP RR failures keep the original routing design."""

from pathlib import Path

import pytest


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
    assert selected
    transit_acl_seen = False

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
                expected_blocked = set()
                for identifier, adjacency in healthy_adjacencies.items():
                    paths = [
                        path
                        for requirement in spec.behavior
                        for path in requirement.paths
                        if (path[0], path[-1])
                        == (adjacency.local_node, adjacency.remote_node)
                    ]
                    assert paths
                    if all(target in path for path in paths):
                        expected_blocked.add(identifier)
                actual_blocked = (
                    set(failure.expected_failed_ids) & healthy_adjacencies.keys()
                )
                assert actual_blocked == expected_blocked
                transit_acl_seen |= any(
                    target not in (adjacency.local_node, adjacency.remote_node)
                    for identifier in actual_blocked
                    if (adjacency := healthy_adjacencies[identifier])
                )
            if case["problem"] == "bgp_hijacking" and any(
                origin.prefix == case["inject"]["target_network"]
                for origin in metric_bound.bgp_plan.originated
            ):
                # A hijacked owned prefix diverts the observer's replies from
                # the original origin to the hijacker.
                prefix = case["inject"]["target_network"]
                observer_id = f"origin.{case['inject']['symptom_host']}.{prefix.replace('/', '-')}"
                assert observer_id in failure.expected_failed_ids
                healthy_by_id = {
                    intent.id: intent for intent in resolved.healthy.intents
                }
                restored_by_id = {
                    intent.id: intent for intent in failure.requirements.intents
                }
                assert healthy_by_id[observer_id].property == "reachability"
                assert all(
                    healthy_by_id[identifier].property == "reachability"
                    and restored_by_id[identifier].property == "isolation"
                    and restored_by_id[identifier].expected == "unreachable"
                    for identifier in failure.expected_failed_ids
                )
                cleanup = failure.recovery_commands[case["inject"]["host_name"]]
                assert any("ip route del local" in command for command in cleanup)
                assert any("ip addr del" in command for command in cleanup)
            elif case["problem"] == "bgp_hijacking":
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

    # The fixed release examples retain coverage of non-endpoint session loss.
    assert transit_acl_seen
