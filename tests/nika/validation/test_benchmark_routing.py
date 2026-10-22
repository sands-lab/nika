"""Benchmark profile preservation and routing requirement regression checks."""

from pathlib import Path

import pytest

from nika.workflows.validation.benchmark_routing import (
    benchmark_isp_profiles,
    build_benchmark_isp_experiment,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.e2e
@pytest.mark.parametrize("scenario", ["isp_dfn-bwin", "isp_di-yuan", "isp_pdh"])
def test_srl_isis_and_as_import_completion(scenario, tmp_path, netcomplete_image):
    from copy import deepcopy

    from nika.workflows.validation.routing import validate_routing_design

    profile = deepcopy(
        benchmark_isp_profiles(
            [
                ROOT / "benchmark/working/cases.yaml",
                ROOT / "benchmark/releases/0.2.0/dev.yaml",
                ROOT / "benchmark/releases/0.2.0/test.yaml",
            ]
        )[f"{scenario}--ospf--ebgp--containerlab"]
    )
    # This is an explicit protocol variant, not a rewritten benchmark task.
    profile["deployment"]["igp"] = "isis"
    profile["benchmark_cases"] = []
    spec = build_benchmark_isp_experiment(profile)
    assert tuple(stage.kind for stage in spec.completion) == (
        "isis_level",
        "bgp_as_imports",
    )
    summary = validate_routing_design(
        spec, tmp_path, runtime=True, image=netcomplete_image
    )
    assert set(summary["synthesis"]["parameters"]) == {"isis_metrics", "bgp_imports"}
    assert (
        summary["runtime"]["healthy"]
        == summary["runtime"]["backbone-link-down-recovered"]
    )
    assert summary["counterexamples"]["business-withdrawal"]["status"] == (
        "rejected-in-runtime"
    )


@pytest.mark.e2e
@pytest.mark.parametrize(
    "scenario,backend",
    [
        ("isp_abilene", "kathara"),
        ("isp_geant", "kathara"),
        ("isp_dfn-bwin", "containerlab"),
    ],
)
def test_as_import_and_igp_completion_preserve_benchmark_failures(
    scenario, backend, tmp_path, netcomplete_image
):
    import json

    from nika.net_env.synthesis.requirements import BgpAsSelectionRequirement
    from nika.workflows.validation.routing import validate_routing_design

    profile = benchmark_isp_profiles(
        [
            ROOT / "benchmark/working/cases.yaml",
            ROOT / "benchmark/releases/0.2.0/dev.yaml",
            ROOT / "benchmark/releases/0.2.0/test.yaml",
        ]
    )[f"{scenario}--ospf--ebgp--{backend}"]
    # Abilene and DFN-BWIN prefer a transit AS over the direct origin AS.
    # This requires synthesized policy to override ordinary AS-path length.
    selections = tuple(
        BgpAsSelectionRequirement(
            kind="bgp_as_selection",
            id=f"exit.as65001.prefix{suffix}",
            asn=65001,
            prefix=f"198.51.{suffix}.0/24",
            candidates=(65002, 65003) if scenario != "isp_geant" else (65002,),
        )
        for suffix in ([101, 102] if scenario != "isp_geant" else [101])
    )
    selections += (
        BgpAsSelectionRequirement(
            kind="bgp_as_selection",
            id="exit.as65002.prefix100",
            asn=65002,
            prefix="198.51.100.0/24",
            candidates=(65001, 65003) if scenario != "isp_geant" else (65001,),
        ),
    )
    spec = build_benchmark_isp_experiment(profile, bgp_selections=selections)
    # Composition and the new behavior survive persisted experiment inputs.
    spec = type(spec).model_validate_json(spec.model_dump_json())
    summary = validate_routing_design(
        spec, tmp_path, runtime=True, image=netcomplete_image
    )
    assert summary["synthesis"]["kind"] == "composite"
    assert summary["synthesis"]["parameters"]["bgp_imports"] > 1
    assert (
        summary["runtime"]["backbone-link-down-recovered"]
        == summary["runtime"]["healthy"]
    )
    assert set(summary.get("failure_compatibility", {})) == {
        f.name for f in spec.failure_checks
    }
    assert (
        summary["counterexamples"]["business-withdrawal"]["status"]
        == "rejected-in-runtime"
    )
    withdrawal = json.loads(
        (tmp_path / "runtime-counterexample-business-withdrawal-0.json").read_text()
    )
    assert any(
        r["intent"].startswith("exit.as65002.prefix100.") and r["status"] == "failed"
        for r in withdrawal["results"]
    )
    solved = json.loads((tmp_path / "routing-parameters.json").read_text())
    # Every physical ingress from one neighboring AS shares the AS solution.
    from nika.problems.rca.inventory import load_offline_net_env

    env = load_offline_net_env(
        scenario, **spec.deployment.model_dump(exclude_none=True)
    )
    remote_asns = {
        (s.local_device, s.remote_ip): (s.local_asn, s.remote_asn)
        for s in env.bgp_plan.sessions
    }
    by_as = {}
    for policy in solved["bgp_imports"]:
        remote = remote_asns[policy["node"], policy["neighbor"]]
        assert by_as.setdefault(remote, policy["terms"]) == policy["terms"]


@pytest.mark.contract
def test_as_import_completion_keeps_special_policy_scope_explicit():
    from nika.net_env.synthesis.requirements import BgpAsSelectionRequirement

    profile = benchmark_isp_profiles([ROOT / "benchmark/working/cases.yaml"])[
        "isp_abilene_ebgp_rpki--ospf--ebgp--kathara"
    ]
    selection = BgpAsSelectionRequirement(
        kind="bgp_as_selection",
        id="exit.original",
        asn=65001,
        prefix="203.0.113.0/24",
        candidates=(65002,),
    )
    with pytest.raises(ValueError, match="fixed security prefix policies"):
        build_benchmark_isp_experiment(profile, bgp_selections=(selection,))


@pytest.mark.contract
@pytest.mark.parametrize("inventory", ["benchmark", "catalogue"])
def test_default_completion_covers_routing_capabilities(inventory):
    from nika.net_env.isp.bgp.policy import fixed_policy_prefixes
    from nika.net_env.synthesis.requirements import resolve_routing_requirements
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.workflows.validation.benchmark_routing import isp_catalog_profiles

    profiles = benchmark_isp_profiles(
        [
            ROOT / "benchmark/working/cases.yaml",
            ROOT / "benchmark/releases/0.2.0/dev.yaml",
            ROOT / "benchmark/releases/0.2.0/test.yaml",
        ]
    )
    if inventory == "catalogue":
        profiles = isp_catalog_profiles()
    completed_bgp = []
    for key, profile in profiles.items():
        spec = build_benchmark_isp_experiment(profile)
        if spec.deployment.bgp_mode != "ebgp":
            assert not isinstance(spec.completion, tuple)
            continue
        env = load_offline_net_env(
            spec.scenario, **spec.deployment.model_dump(exclude_none=True)
        )
        selections = [r for r in spec.behavior if r.kind == "bgp_as_selection"]
        assert selections
        assert {r.prefix for r in selections}.isdisjoint(
            fixed_policy_prefixes(env.bgp_plan)
        )
        assert isinstance(spec.completion, tuple)
        assert set(spec.completion[1].requirements) == {r.id for r in selections}
        resolved = resolve_routing_requirements(spec, env)
        assert {
            i.id
            for i in resolved.healthy.intents
            if i.bgp_route and i.id.startswith("exit.")
        }
        completed_bgp.append(key)
    assert completed_bgp


@pytest.mark.contract
def test_as_selection_predicates_follow_original_order_before_synthesis():
    from nika.net_env.synthesis.requirements import (
        BgpAsSelectionRequirement,
        resolve_routing_requirements,
    )
    from nika.problems.rca.inventory import load_offline_net_env

    profile = benchmark_isp_profiles([ROOT / "benchmark/working/cases.yaml"])[
        "isp_abilene--ospf--ebgp--kathara"
    ]
    env = load_offline_net_env(profile["scenario"], **profile["deployment"])
    for order in ((65002, 65003), (65003, 65002)):
        requirement = BgpAsSelectionRequirement(
            kind="bgp_as_selection",
            id="exit.original",
            asn=65001,
            prefix="198.51.102.0/24",
            candidates=order,
        )
        original = build_benchmark_isp_experiment(
            profile, bgp_selections=(requirement,)
        )
        resolved = resolve_routing_requirements(original, env)
        checks = [
            i for i in resolved.healthy.intents if i.id.startswith("exit.original.")
        ]
        assert {i.bgp_route.node for i in checks} == {
            n.device_name for n in env.bgp_plan.nodes if n.asn == requirement.asn
        }
        assert {i.bgp_route.exit_asn for i in checks} == {order[0]}


@pytest.mark.contract
def test_benchmark_profiles_preserve_protocol_and_backend_choices():
    profiles = benchmark_isp_profiles([ROOT / "benchmark/working/cases.yaml"])
    native = profiles["isp_dfn-gwin--isis--ibgp_rr--containerlab"]
    assert native["deployment"]["igp"] == "isis"
    assert native["supported"]
    assert build_benchmark_isp_experiment(native).routing_model == "srl-isp"
    fixed = profiles["isp_abilene_ebgp_rpki--ospf--ebgp--kathara"]
    assert fixed["deployment"]["rpki"] is True
    assert fixed["features"] == ["rpki"]
    assert fixed["supported"]
    assert build_benchmark_isp_experiment(fixed).failure_checks[0].static_edits
    profile = profiles["isp_polska--ospf--none--kathara"]
    spec = build_benchmark_isp_experiment(profile)
    assert spec.deployment.igp == "ospf"
    assert spec.deployment.bgp_mode == "none"
    assert spec.deployment.backend == "kathara"
    assert spec.completion.kind == "ospf_area"
    from nika.net_env.synthesis.requirements import resolve_routing_requirements
    from nika.problems.rca.inventory import load_offline_net_env

    resolved = resolve_routing_requirements(
        spec,
        load_offline_net_env(
            spec.scenario, **spec.deployment.model_dump(exclude_none=True)
        ),
    )
    assert any(
        f.problem == "ospf_area_misconfiguration"
        and f.params == {"host_name": "bialystok"}
        for f in spec.failure_checks
    )
    assert set(spec.runtime_counterexamples[0].expected_failed_ids) <= {
        i.id for i in resolved.healthy.intents
    }
    # Isolating the anchor must not imply that every intermediate route disappears.
    fault = resolved.failure_checks[0]
    surviving = {
        i.id: i.route_next_hops for i in fault.requirements.intents if i.route_next_hops
    }
    unchanged = {
        i.id
        for i in resolved.healthy.intents
        if i.route_next_hops and i.route_next_hops == surviving.get(i.id)
    }
    assert unchanged
    assert set(fault.expected_failed_ids).isdisjoint(unchanged)


@pytest.mark.unit
@pytest.mark.parametrize(
    "address,status", [("10.0.0.1", "passed"), ("10.0.1.1", "failed")]
)
@pytest.mark.parametrize("router_id", ["192.0.2.2", "10.0.0.1"])
def test_ospf_runtime_distinguishes_parallel_links(address, status, router_id):
    from nika.net_env.contract import (
        AdjacencyExpectation,
        ValidationContract,
        ValidationIntent,
    )
    from nika.net_env.isp.kathara.verify import verify_isp_contract
    from nika.net_env.verify_collection import CommandResult, VerifyCollection

    intent = ValidationIntent(
        id="adj",
        description="The specified parallel link must form a full adjacency.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="ospf",
            local_node="r1",
            remote_node="r2",
            local_address="10.0.0.0",
            remote_address=address,
            local_router_id="192.0.2.1",
            remote_router_id=router_id,
        ),
    )
    contract = ValidationContract(
        contract_id="parallel",
        scenario="isp",
        design_source={"basis": "declared parallel point-to-point links"},
        intents=(intent,),
    )
    output = f"{router_id} 1 Full/DR 00:00:30 10.0.0.1 eth0\n{router_id} 1 Init/DROther 00:00:30 10.0.1.1 eth1\n"
    runtime = VerifyCollection(
        {("r1", "vtysh -c 'show ip ospf neighbor'"): CommandResult(output, 0)},
        ["r1"],
        0,
    )
    report = verify_isp_contract(runtime, contract=contract, plan=None)
    assert report.results[0].status == status


@pytest.mark.unit
@pytest.mark.parametrize("present", [False, True])
def test_runtime_bgp_presence_and_origin(present):
    import json

    from nika.net_env.contract import (
        BgpRouteExpectation,
        ValidationContract,
        ValidationIntent,
    )
    from nika.net_env.isp.kathara.verify import verify_isp_contract
    from nika.net_env.isp.verify_support import intent_command
    from nika.net_env.verify_collection import CommandResult, VerifyCollection

    intent = ValidationIntent(
        id="origin",
        description="Accept the declared route presence and originating AS.",
        property="bgp_route",
        expected="selected" if present else "absent",
        bgp_route=BgpRouteExpectation(
            node="r1",
            prefix="198.51.100.0/24",
            present=present,
            origin_asn=65001 if present else None,
        ),
    )
    contract = ValidationContract(
        contract_id="origin",
        scenario="isp",
        design_source={"basis": "fixed origin authorization"},
        intents=(intent,),
    )
    request = intent_command(intent)
    output = {"paths": [{"bestpath": True, "path": "65000 65001"}]} if present else {}
    observations = VerifyCollection(
        {(request.node, request.command): CommandResult(json.dumps(output), 0)},
        ["r1"],
        0,
    )
    result = verify_isp_contract(observations, contract=contract, plan=None).results[0]
    assert result.status == "passed"
    assert result.evidence["observed_present"] == present


@pytest.mark.contract
@pytest.mark.parametrize(
    "scenario", ["isp_abilene_ebgp_rtbh", "isp_dfn-bwin_ebgp_rtbh"]
)
def test_rtbh_echo_requirement_names_the_original_active_endpoint(scenario):
    from nika.problems.rca.inventory import load_offline_net_env

    profile = benchmark_isp_profiles([ROOT / "benchmark/working/cases.yaml"])[
        f"{scenario}--ospf--ebgp--kathara"
    ]
    spec = build_benchmark_isp_experiment(profile)
    env = load_offline_net_env(scenario)
    origin = next(
        o
        for o in env.bgp_plan.originated
        if o.prefix == env.bgp_plan.inventory["target_prefix"]
    )
    intent = next(i for i in spec.healthy.intents if i.id.startswith("rtbh.provider."))
    assert intent.destination.kind == "node"
    assert intent.destination.name == origin.device
    assert intent.destination.address == origin.ping_address
    assert intent.traffic.round_trip


@pytest.mark.e2e
@pytest.mark.parametrize(
    "profile_key",
    [
        key
        for key, profile in benchmark_isp_profiles(
            [
                ROOT / "benchmark/working/cases.yaml",
                ROOT / "benchmark/releases/0.2.0/dev.yaml",
                ROOT / "benchmark/releases/0.2.0/test.yaml",
            ]
        ).items()
        if profile["supported"]
    ],
)
def test_benchmark_isp_configuration_execution_and_recovery(
    profile_key, tmp_path, netcomplete_image
):
    from nika.workflows.validation.routing import validate_routing_design

    manifests = [
        ROOT / "benchmark/working/cases.yaml",
        ROOT / "benchmark/releases/0.2.0/dev.yaml",
        ROOT / "benchmark/releases/0.2.0/test.yaml",
    ]
    profile = benchmark_isp_profiles(manifests)[profile_key]
    spec = build_benchmark_isp_experiment(profile)
    summary = validate_routing_design(
        spec, tmp_path, runtime=True, image=netcomplete_image
    )
    assert summary["synthesis"]["backend"] == "netcomplete"
    assert summary["synthesis"]["parameters"][
        "isis_metrics" if spec.deployment.igp == "isis" else "ospf_costs"
    ] == sum(
        1
        for i in spec.healthy.intents
        if i.adjacency and i.adjacency.protocol == spec.deployment.igp
    )
    assert (
        summary["runtime"]["backbone-link-down-recovered"]
        == summary["runtime"]["healthy"]
    )
    assert set(summary.get("failure_compatibility", {})) == {
        f.name for f in spec.failure_checks
    }
    assert (
        summary["counterexamples"][spec.runtime_counterexamples[0].name]["status"]
        == "rejected-in-runtime"
    )


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["isp_sun", "isp_ta1"])
def test_parallel_ospf_paths_bind_every_physical_next_hop(scenario):
    from nika.net_env.synthesis.requirements import resolve_routing_requirements
    from nika.problems.rca.inventory import load_offline_net_env

    profiles = benchmark_isp_profiles([ROOT / "benchmark/working/cases.yaml"])
    spec = build_benchmark_isp_experiment(profiles[f"{scenario}--ospf--none--kathara"])
    env = load_offline_net_env(
        scenario, **spec.deployment.model_dump(exclude_none=True)
    )
    resolved = resolve_routing_requirements(spec, env)
    link = next(
        edge
        for edge in env.plan.links
        if sum(
            {other.endpoint_a, other.endpoint_b} == {edge.endpoint_a, edge.endpoint_b}
            for other in env.plan.links
        )
        > 1
    )
    siblings = [
        edge
        for edge in env.plan.links
        if {edge.endpoint_a, edge.endpoint_b} == {link.endpoint_a, link.endpoint_b}
    ]
    assert len(siblings) > 1
    from nika.net_env.synthesis.requirements import link_state_paths_intents

    remaining = [
        i.adjacency
        for i in spec.healthy.intents
        if i.adjacency
        and {i.adjacency.local_address, i.adjacency.remote_address}
        != {link.address_a, link.address_b}
    ]
    changed_intents = [
        i
        for requirement in spec.behavior
        for i in link_state_paths_intents(requirement, requirement.paths, remaining)
    ]
    for endpoint in (link.endpoint_a, link.endpoint_b):
        lost = link.address_b if endpoint == link.endpoint_a else link.address_a
        routes = [
            i.route_next_hops
            for i in resolved.healthy.intents
            if i.route_next_hops
            and i.route_next_hops.node == endpoint
            and lost in i.route_next_hops.next_hops
        ]
        assert routes
        for route in routes:
            changed = next(
                i.route_next_hops
                for i in changed_intents
                if i.route_next_hops
                and (i.route_next_hops.node, i.route_next_hops.prefix)
                == (route.node, route.prefix)
            )
            assert set(changed.next_hops) == set(route.next_hops) - {lost}


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["isp_abilene", "isp_geant"])
def test_benchmark_bgp_preserves_as_boundaries_and_fault_targets(scenario):
    from nika.net_env.synthesis.requirements import resolve_routing_requirements
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.net_env.isp.igp.plan import active_igp_links
    from nika.net_env.net_env_pool import get_net_env_instance

    profile = benchmark_isp_profiles([ROOT / "benchmark/working/cases.yaml"])[
        f"{scenario}--ospf--ebgp--kathara"
    ]
    spec = build_benchmark_isp_experiment(profile)
    env = load_offline_net_env(
        scenario, **spec.deployment.model_dump(exclude_none=True)
    )
    original = env.plan
    asn_of = {node.device_name: node.asn for node in env.bgp_plan.nodes}
    assert any(
        i.adjacency and i.adjacency.route_reflector_client for i in spec.healthy.intents
    )
    assert len([i for i in spec.healthy.intents if i.id.startswith("business.")]) == 6
    assert spec.failure_checks[0].problem == "bgp_max_prefix_exceeded"
    assert len(spec.failure_checks[0].expected_failed_ids) == 2
    assert spec.counterexamples[0].name == "missing-business-announcement"
    active = active_igp_links(original)
    assert all(asn_of[link.endpoint_a] == asn_of[link.endpoint_b] for link in active)
    parameters = {
        "ospf_costs": [
            dict(node=node, interface=interface, cost=1)
            for link in active
            for node, interface in [
                (link.endpoint_a, link.iface_a),
                (link.endpoint_b, link.iface_b),
            ]
        ]
    }
    completed = get_net_env_instance(
        scenario,
        **spec.deployment.model_dump(exclude_none=True),
        routing_parameters=parameters,
    )
    assert completed.bgp_plan == env.bgp_plan
    assert {
        (link.link_id, link.endpoint_a, link.endpoint_b, link.address_a, link.address_b)
        for link in completed.plan.links
    } == {
        (link.link_id, link.endpoint_a, link.endpoint_b, link.address_a, link.address_b)
        for link in original.links
    }
    assert all(link.metric == 1 for link in active_igp_links(completed.plan))
    inactive = {
        link.link_id: link.metric for link in original.links if link not in active
    }
    assert all(
        link.metric == inactive[link.link_id]
        for link in completed.plan.links
        if link.link_id in inactive
    )
    resolved = resolve_routing_requirements(spec, env)
    assert len(resolved.behavior) > 1


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["isp_abilene", "isp_dfn-bwin", "isp_ta2"])
def test_single_level_isis_completion_preserves_original_sketch(scenario):
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.net_env.isp.igp.plan import active_igp_links
    from nika.net_env.synthesis.requirements import resolve_routing_requirements

    manifests = [
        ROOT / "benchmark/working/cases.yaml",
        ROOT / "benchmark/releases/0.2.0/dev.yaml",
        ROOT / "benchmark/releases/0.2.0/test.yaml",
    ]
    profile = benchmark_isp_profiles(manifests)[f"{scenario}--isis--none--kathara"]
    spec = build_benchmark_isp_experiment(profile)
    options = spec.deployment.model_dump(exclude_none=True)
    original = get_net_env_instance(scenario, **options)
    edges = active_igp_links(original.plan)
    completed = get_net_env_instance(
        scenario,
        **options,
        routing_parameters={
            "isis_metrics": [
                dict(node=n, interface=i, metric=1)
                for edge in edges
                for n, i in [
                    (edge.endpoint_a, edge.iface_a),
                    (edge.endpoint_b, edge.iface_b),
                ]
            ],
        },
    )
    assert completed.igp == original.igp == "isis"
    assert completed.bgp_plan == original.bgp_plan is None
    assert {
        (e.link_id, e.endpoint_a, e.endpoint_b, e.address_a, e.address_b)
        for e in completed.plan.links
    } == {
        (e.link_id, e.endpoint_a, e.endpoint_b, e.address_a, e.address_b)
        for e in original.plan.links
    }
    assert all(e.metric == 1 for e in active_igp_links(completed.plan))
    assert all(
        "router isis CORE" in c and "router ospf" not in c
        for c in completed.deployment_configs.values()
    )
    resolved = resolve_routing_requirements(spec, original)
    assert spec.completion.kind == "isis_level"
    assert all(
        i.adjacency.protocol == "isis" and i.adjacency.local_interface
        for i in spec.healthy.intents
        if i.adjacency
    )
    assert any(i.route_next_hops for i in resolved.healthy.intents)
    if any(c["problem"] == "link_down" for c in profile["benchmark_cases"]):
        params = next(
            c["inject"]
            for c in profile["benchmark_cases"]
            if c["problem"] == "link_down"
        )
        assert spec.incidents[0].host_name == params["host_name"]
        assert spec.incidents[0].intf_name == params["intf_name"]
