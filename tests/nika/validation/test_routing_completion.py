"""Specification-driven routing completion preserves executable failure behavior."""

from pathlib import Path

import pytest

from tests.support.scenario_validation import assert_scenario_acceptance
from pydantic import ValidationError

from nika.net_env.net_env_pool import get_net_env_instance
from nika.net_env.synthesis.parameters import RoutingParameters
from nika.workflows.validation.scenario import validate_scenario_design
from nika.workflows.validation.scenario_spec import ScenarioExperiment

SPECIMENS = Path(__file__).resolve().parents[3] / "experiment/scenario_validation"


@pytest.mark.contract
def test_reject_parameters_outside_scenario_capabilities():
    # An OSPF cost must never silently disappear in an eBGP-only constructor.
    with pytest.raises(ValueError, match="scenario capabilities"):
        get_net_env_instance(
            "dc_clos",
            topo_size="s",
            routing_parameters={
                "ospf_costs": [
                    {"node": "leaf_router_0_0", "interface": "eth0", "cost": 10}
                ],
            },
        )


@pytest.mark.unit
def test_reject_conflicting_parameter_bindings():
    cost = {"node": "core", "interface": "eth0", "cost": 10}
    with pytest.raises(ValidationError, match="distinct targets"):
        RoutingParameters(ospf_costs=[cost, {**cost, "cost": 20}])


@pytest.mark.contract
def test_isp_as_policy_rejects_partial_border_binding_and_unsupported_vendor():
    from nika.net_env.synthesis.parameters import bind_isp_imports

    env = get_net_env_instance("isp_abilene", backend="kathara", bgp_mode="ebgp")
    sessions = [
        s
        for s in env.bgp_plan.sessions
        if (s.local_asn, s.remote_asn) == (65001, 65002)
    ]
    assert len(sessions) > 1
    policy = {
        "node": sessions[0].local_device,
        "neighbor": sessions[0].remote_ip,
        "terms": [
            {"prefix": "198.51.101.0/24", "permit": True, "local_preference": 10}
        ],
    }
    parameters = {"bgp_imports": [policy]}
    with pytest.raises(ValueError, match="all borders consistently"):
        bind_isp_imports(env.bgp_plan, parameters)
    with pytest.raises(ValueError, match="ISP capabilities"):
        get_net_env_instance(
            "isp_abilene",
            backend="containerlab",
            bgp_mode="ebgp",
            routing_parameters={
                "bgp_exports": [
                    {
                        **policy,
                        "terms": [{"prefix": "198.51.101.0/24", "permit": True}],
                    }
                ]
            },
        )


@pytest.mark.contract
def test_original_path_generates_checks_for_every_hop():
    from nika.net_env.synthesis.requirements import resolve_routing_requirements

    spec = ScenarioExperiment.model_validate_json(
        (SPECIMENS / "campus.json").read_text()
    )
    resolved = resolve_routing_requirements(
        spec, get_net_env_instance(spec.scenario, topo_size=spec.topo_size)
    )
    checks = [
        i for i in resolved.healthy.intents if i.id.startswith("path.server.core1.")
    ]
    assert [i.route_next_hops.node for i in checks] == [
        "router_core_1",
        "router_core_2",
        "router_core_3",
    ]
    assert [i.route_next_hops.next_hops for i in checks] == [
        ("172.16.0.1",),
        ("172.16.0.5",),
        ("172.16.0.11",),
    ]
    incident = next(i for i in resolved.incidents if i.name == "core-link-down")
    checks = [
        i
        for i in incident.requirements.intents
        if i.id.startswith("path.server.core1.")
    ]
    assert [i.route_next_hops.node for i in checks] == [
        "router_core_1",
        "router_core_3",
    ]


@pytest.mark.contract
def test_original_bgp_order_drives_healthy_and_failed_selection():
    from nika.net_env.synthesis.requirements import resolve_routing_requirements

    spec = ScenarioExperiment.model_validate_json(
        (SPECIMENS / "enterprise.json").read_text()
    )
    env = get_net_env_instance(spec.scenario, topo_size=spec.topo_size)
    resolved = resolve_routing_requirements(spec, env)
    healthy = next(
        i
        for i in resolved.runtime_requirements.intents
        if i.id == "route.business.hq.br1"
    )
    faulty = next(
        i
        for i in resolved.incidents[0].runtime_requirements.intents
        if i.id == healthy.id
    )
    assert healthy.route_next_hops.next_hops == ("172.30.0.10",)
    assert faulty.route_next_hops.next_hops == ("172.30.0.14",)
    # Changing the original ordering must change the predicate, before synthesis.
    behavior = [
        r.model_copy(update={"candidates": tuple(reversed(r.candidates))})
        if r.id == healthy.id
        else r
        for r in spec.behavior
    ]
    changed = resolve_routing_requirements(
        spec.model_copy(update={"behavior": tuple(behavior)}), env
    )
    actual = next(i for i in changed.runtime_requirements.intents if i.id == healthy.id)
    assert actual.route_next_hops == faulty.route_next_hops


@pytest.mark.unit
def test_state_path_preserves_original_endpoints():
    from nika.net_env.synthesis.requirements import OspfPathRequirement

    with pytest.raises(ValidationError, match="original endpoints"):
        OspfPathRequirement(
            kind="ospf_path",
            id="path",
            area=0,
            prefix="10.0.0.0/24",
            path=["a", "b"],
            state_paths={"down": ["a", "c"]},
        )


@pytest.mark.contract
def test_static_counterexample_references_original_requirements(tmp_path):
    spec = ScenarioExperiment.model_validate_json((SPECIMENS / "clos.json").read_text())
    counterexample = spec.counterexamples[0].model_copy(
        update={"expected_failed_ids": ("undeclared-requirement",)}
    )
    spec = spec.model_copy(update={"counterexamples": (counterexample,)})
    with pytest.raises(ValueError, match="original static requirement IDs"):
        validate_scenario_design(spec, tmp_path)
    assert not (tmp_path / "netcomplete").exists()
    assert not (tmp_path / "summary.json").exists()


@pytest.mark.contract
def test_runtime_requirements_cannot_redefine_a_static_predicate(tmp_path):
    spec = ScenarioExperiment.model_validate_json((SPECIMENS / "clos.json").read_text())
    extra = spec.runtime_requirements.intents[0].model_copy(
        update={"id": spec.healthy.intents[0].id}
    )
    spec = spec.model_copy(
        update={
            "runtime_requirements": spec.runtime_requirements.model_copy(
                update={"intents": (extra,)}
            )
        }
    )
    with pytest.raises(ValueError, match="distinct original IDs"):
        validate_scenario_design(spec, tmp_path)
    assert not (tmp_path / "summary.json").exists()


@pytest.mark.e2e
@pytest.mark.parametrize("config_generator", [None, "netlab"])
@pytest.mark.parametrize(
    "specimen", ["clos", "clos-m", "campus", "enterprise", "min3clos"]
)
def test_completion_configuration_execution_and_registered_failures(
    specimen, config_generator, tmp_path, netcomplete_image, batfish_verifier
):
    if config_generator:
        from importlib.util import find_spec

        if find_spec("netsim") is None:
            pytest.skip("Install the netlab extra for upstream construction")
    spec = ScenarioExperiment.model_validate_json(
        (SPECIMENS / f"{specimen}.json").read_text()
    )
    summary = validate_scenario_design(
        spec,
        tmp_path,
        runtime=True,
        image=netcomplete_image,
        config_generator=config_generator,
        batfish_verifier=batfish_verifier,
    )
    assert_scenario_acceptance(spec, summary, tmp_path, runtime=True)
    assert summary["synthesis"]["backend"] == "netcomplete"


@pytest.mark.integration
def test_inconsistent_paths_receive_explicit_solver_unsat(tmp_path, netcomplete_image):
    from nika.net_env.synthesis.runner import run_netcomplete

    payload = dict(
        kind="ospf_area",
        routers=["a", "b", "c"],
        links=[
            dict(id=a + b, source=a, target=b)
            for a, b in [("a", "b"), ("a", "c"), ("b", "c")]
        ],
        paths=[["a", "b", "c"], ["a", "c", "b"]],
    )
    with pytest.raises(RuntimeError, match="NetComplete failed"):
        run_netcomplete(payload, tmp_path, image=netcomplete_image)
    log = (tmp_path / "solver.log").read_text()
    assert "sat result: unsat" in log
    assert "NetComplete could not satisfy the original paths" in log
    assert not (tmp_path / "solution.json").exists()


@pytest.mark.contract
def test_reject_duplicate_authored_and_derived_routing_predicates():
    from nika.net_env.synthesis.requirements import resolve_routing_requirements

    spec = ScenarioExperiment.model_validate_json(
        (SPECIMENS / "campus.json").read_text()
    )
    env = get_net_env_instance(spec.scenario, topo_size=spec.topo_size)
    resolved = resolve_routing_requirements(spec, env)
    duplicated = spec.model_copy(update={"healthy": resolved.healthy})
    with pytest.raises(ValueError, match="duplicate manually authored"):
        resolve_routing_requirements(duplicated, env)


@pytest.mark.contract
def test_reject_unknown_candidate_in_original_bgp_order():
    from nika.net_env.synthesis.requirements import resolve_routing_requirements

    spec = ScenarioExperiment.model_validate_json(
        (SPECIMENS / "enterprise.json").read_text()
    )
    original = spec.behavior[0]
    invalid = original.model_copy(
        update={"candidates": ("unknown-peer", *original.candidates)}
    )
    spec = spec.model_copy(update={"behavior": (invalid, *spec.behavior[1:])})
    env = get_net_env_instance(spec.scenario, topo_size=spec.topo_size)
    with pytest.raises(ValueError, match="unknown sketch neighbor"):
        resolve_routing_requirements(spec, env)


@pytest.mark.contract
@pytest.mark.parametrize("feature", ["igp", "inherited-policy", "vrf"])
def test_srl_projection_rejects_features_outside_its_model(feature):
    from nika.validation.batfish.srl import project_srl_bgp

    env = get_net_env_instance("min3clos", routing_model="srl-bgp")
    document = env.routing_documents["leaf1"]
    instance = document["srl_nokia-network-instance:network-instance"][0]
    if feature == "igp":
        instance["protocols"]["srl_nokia-ospf:ospf"] = {}
    elif feature == "inherited-policy":
        instance["protocols"]["srl_nokia-bgp:bgp"]["group"][0]["import-policy"] = [
            "OTHER"
        ]
    else:
        instance["name"] = "business"
    with pytest.raises(ValueError, match="projection"):
        project_srl_bgp("leaf1", document)


@pytest.mark.integration
def test_ospf_states_cannot_choose_inconsistent_costs(tmp_path, netcomplete_image):
    from nika.net_env.synthesis.runner import run_netcomplete

    pairs = [("a", "b"), ("a", "c"), ("b", "c")]
    payload = dict(
        kind="ospf_area",
        routers=[f"s{state}_{node}" for state in (0, 1) for node in "abc"],
        links=[
            dict(id=f"{state}:{a}{b}", source=f"s{state}_{a}", target=f"s{state}_{b}")
            for state in (0, 1)
            for a, b in pairs
        ],
        cost_groups=[[f"0:{a}{b}", f"1:{a}{b}"] for a, b in pairs],
        paths=[["s0_a", "s0_b", "s0_c"], ["s1_a", "s1_c", "s1_b"]],
    )
    with pytest.raises(RuntimeError, match="NetComplete failed"):
        run_netcomplete(payload, tmp_path, image=netcomplete_image)
    assert "sat result: unsat" in (tmp_path / "solver.log").read_text()
    assert not (tmp_path / "solution.json").exists()


@pytest.mark.integration
def test_ospf_ecmp_completes_equal_cost_paths_of_different_lengths(
    tmp_path, netcomplete_image
):
    from nika.net_env.synthesis.runner import run_netcomplete

    result = run_netcomplete(
        dict(
            kind="ospf_area",
            routers=["a", "b", "c"],
            links=[
                dict(id=a + b, source=a, target=b)
                for a, b in [("a", "b"), ("a", "c"), ("b", "c")]
            ],
            paths=[],
            ecmp_paths=[[["a", "c"], ["a", "b", "c"]]],
        ),
        tmp_path,
        image=netcomplete_image,
    )
    assert result["ac"] == result["ab"] + result["bc"]
    assert all(1 <= cost <= 65535 for cost in result.values())
