"""Native ISP sketches share metric completion and original behavioral controls."""

from pathlib import Path

import pytest
import yaml

from nika.net_env.isp.igp.plan import active_igp_links
from nika.net_env.net_env_pool import get_net_env_instance
from nika.net_env.synthesis.requirements import resolve_routing_requirements
from nika.problems.rca.inventory import load_offline_net_env
from nika.validation.batfish.srl_isp import project_srl_isp, srl_addresses
from nika.workflows.validation.benchmark_routing import (
    benchmark_isp_profiles,
    isp_catalog_profiles,
    build_benchmark_isp_experiment,
)

ROOT = Path(__file__).resolve().parents[3]
PROFILES = benchmark_isp_profiles(
    [
        ROOT / "benchmark/working/cases.yaml",
        ROOT / "benchmark/releases/0.2.0/dev.yaml",
        ROOT / "benchmark/releases/0.2.0/test.yaml",
    ]
)
NATIVE = {
    k: v
    for k, v in PROFILES.items()
    if v["deployment"]["device_profile"] == "nokia_srlinux"
}


@pytest.mark.contract
@pytest.mark.parametrize("key", NATIVE)
def test_native_isp_metric_binding_and_controls_preserve_original_sketch(key):
    profile = NATIVE[key]
    spec = build_benchmark_isp_experiment(profile)
    options = spec.deployment.model_dump(exclude_none=True)
    original = load_offline_net_env(spec.scenario, **options)
    field = "isis_metrics" if original.igp == "isis" else "ospf_costs"
    value = "metric" if original.igp == "isis" else "cost"
    parameters = {
        field: [
            {"node": node, "interface": iface, value: 3}
            for edge in active_igp_links(original.plan)
            for node, iface in [
                (edge.endpoint_a, edge.iface_a),
                (edge.endpoint_b, edge.iface_b),
            ]
        ]
    }
    candidate = get_net_env_instance(
        spec.scenario, **options, routing_parameters=parameters
    )
    assert candidate.bgp_plan == original.bgp_plan
    assert all(e.metric == 3 for e in active_igp_links(candidate.plan))
    assert [
        (e.link_id, e.endpoint_a, e.endpoint_b, e.address_a, e.address_b)
        for e in candidate.plan.links
    ] == [
        (e.link_id, e.endpoint_a, e.endpoint_b, e.address_a, e.address_b)
        for e in original.plan.links
    ]
    for node, document in candidate.routing_documents.items():
        assert srl_addresses(document) == srl_addresses(
            original.routing_documents[node]
        )
        project_srl_isp(node, document)
    resolved = resolve_routing_requirements(spec, original)
    assert any(i.route_next_hops for i in resolved.healthy.intents)
    for mutation in spec.counterexamples:
        config = yaml.safe_dump(
            candidate.routing_documents[mutation.node], sort_keys=False
        )
        assert config.count(mutation.old) == 1
        changed = yaml.safe_load(config.replace(mutation.old, mutation.new, 1))
        assert project_srl_isp(mutation.node, changed) != project_srl_isp(
            mutation.node, candidate.routing_documents[mutation.node]
        )
    for failure in spec.failure_checks:
        for node, (old, new) in failure.static_edits.items():
            config = yaml.safe_dump(candidate.routing_documents[node], sort_keys=False)
            assert config.count(old) == 1
            project_srl_isp(node, yaml.safe_load(config.replace(old, new, 1)))
    originals = {
        (c["problem"], tuple(sorted(c["inject"].items())))
        for c in profile["benchmark_cases"]
    }
    assert all(
        (f.problem, tuple(sorted(f.params.items()))) in originals
        for f in spec.failure_checks
    )


@pytest.mark.unit
@pytest.mark.parametrize("protocol", ["isis", "ospf"])
def test_srl_isp_projection_rejects_unmodeled_protocol_attributes(protocol):
    env = load_offline_net_env(
        "isp_dfn-bwin", backend="containerlab", igp=protocol, bgp_mode="none"
    )
    document = env.routing_documents["berlin"]
    key = "srl_nokia-isis:isis" if protocol == "isis" else "srl_nokia-ospf:ospf"
    document["srl_nokia-network-instance:network-instance"][0]["protocols"][key][
        "instance"
    ][0]["unmodeled-option"] = True
    with pytest.raises(ValueError, match="Unsupported SR Linux attributes"):
        project_srl_isp("berlin", document)


@pytest.mark.contract
def test_isp_catalog_profiles_cover_registered_defaults():
    from nika.net_env.net_env_pool import list_all_net_envs
    from nika.net_env.isp.identity import is_isp_scenario

    catalog = isp_catalog_profiles()
    assert {p["scenario"] for p in catalog.values()} == {
        n for n in list_all_net_envs() if is_isp_scenario(n)
    }
    profile = catalog["isp_aarnet--isis--none--kathara"]
    assert not profile["benchmark_cases"]
    spec = build_benchmark_isp_experiment(profile)
    assert spec.scenario == "isp_aarnet"
    assert spec.deployment.igp == "isis"
    assert spec.completion.kind == "isis_level"
