"""The three fabric implementations preserve one design and communication contract."""

import pytest

from nika.problems.rca.inventory import load_offline_net_env


@pytest.mark.unit
@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_clos_implementations_share_design_and_requirements(size):
    environments = [
        load_offline_net_env(scenario, topo_size=size)
        for scenario in ("dc_clos", "sdn_l3_clos", "p4_dc_fabric")
    ]
    reference = environments[0]
    for env in environments:
        assert env.model.endpoints == reference.model.endpoints
        assert env.model.leaf_spine_links == reference.model.leaf_spine_links
        assert env.model.web_urls == reference.model.web_urls
        assert env.validation_contract.intents == reference.validation_contract.intents
        assert any(i.expected == "unreachable" for i in env.validation_contract.intents)
        # Check the deployed data interfaces, rather than only comparing models.
        for node, ports in reference.model.ports.items():
            machine = env.lab.machines[node]
            for index, port in enumerate(ports):
                actual = machine.interfaces[index].link
                peer = env.lab.machines[port.peer]
                assert any(
                    interface.link == actual for interface in peer.interfaces.values()
                )


@pytest.mark.contract
@pytest.mark.parametrize(
    ("specimen", "size", "profile"),
    [
        ("clos", "s", "frr"),
        ("clos-m", "m", "frr"),
        ("clos-iosxr", "s", "iosxr"),
        ("clos-routeros", "s", "routeros"),
    ],
)
def test_routing_specimens_follow_shared_original_requirements(specimen, size, profile):
    from pathlib import Path

    from nika.net_env.clos.design import build_clos_fabric_model
    from nika.net_env.clos.requirements import communication_contract
    from nika.workflows.validation.clos import build_routing_clos_experiment
    from nika.workflows.validation.scenario_spec import ScenarioExperiment

    spec = build_routing_clos_experiment(size, device_profile=profile)
    path = (
        Path(__file__).resolve().parents[3]
        / "tests/fixtures/scenario_validation"
        / f"{specimen}.json"
    )
    assert ScenarioExperiment.model_validate_json(path.read_text()) == spec
    required = communication_contract(
        build_clos_fabric_model(size), scenario="dc_clos", topo_size=size
    )
    actual = {i.id: i for i in spec.healthy.intents}
    assert all(actual[i.id] == i for i in required.intents)
    assert any(i.property == "adjacency" for i in spec.healthy.intents)


@pytest.mark.unit
def test_uplink_outage_changes_paths_at_both_ends_of_a_rack_pair():
    from nika.workflows.validation.clos import build_routing_clos_experiment

    spec = build_routing_clos_experiment()
    healthy = {
        i.route_next_hops.node: set(i.route_next_hops.next_hops)
        for i in spec.runtime_requirements.intents
    }
    down = {
        i.route_next_hops.node: set(i.route_next_hops.next_hops)
        for i in spec.incidents[0].runtime_requirements.intents
    }
    assert healthy == {
        "leaf_1": {"172.16.0.1", "172.16.0.3"},
        "leaf_2": {"172.16.0.5", "172.16.0.7"},
    }
    assert down == {"leaf_1": {"172.16.0.3"}, "leaf_2": {"172.16.0.7"}}


@pytest.mark.unit
@pytest.mark.parametrize("scenario", ["dc_clos", "sdn_l3_clos", "p4_dc_fabric"])
def test_endpoint_faults_probe_a_remote_rack(scenario):
    from nika.validation.symptom.probe import _resolve_path

    env = load_offline_net_env(scenario, topo_size="s")
    for endpoint in env.model.client_endpoints():
        path = _resolve_path(scenario, {"host_name": endpoint.name})
        destination = next(e for e in env.model.endpoints if e.ip == path.dst_ip)
        control = next(e for e in env.model.endpoints if e.name == path.peer_host)
        assert path.src_host == endpoint.name
        assert destination.leaf_id != endpoint.leaf_id
        assert control.leaf_id not in {endpoint.leaf_id, destination.leaf_id}


@pytest.mark.contract
@pytest.mark.parametrize("release", ["0.1.0", "0.2.0"])
def test_published_clos_keeps_original_resource_names(release):
    from nika.net_env.net_env_pool import get_net_env_instance

    env = get_net_env_instance("dc_clos", topo_size="s", environment_release=release)
    env.load_machines()
    assert "client_0" in env.hosts
    assert "dns_pod0" in env.lab.machines
    assert "leaf_router_0_0" in env.routers
    from nika.problems.registry import get_problem_instance

    problem = get_problem_instance(["host_missing_ip"], "dc_clos", release=release)
    assert "client_0" in problem.net_env.hosts
    from nika.net_env.net_env_pool import get_probe_path

    assert get_probe_path("dc_clos", net_env=problem.net_env).src_host == "client_0"
    current = get_net_env_instance("dc_clos", topo_size="s")
    assert "client_1_1" in current.hosts
    assert "client_0" not in current.lab.machines


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["sdn_l3_clos", "p4_dc_fabric"])
def test_published_packet_fabric_keeps_original_port_order(scenario):
    env = load_offline_net_env(scenario, "s", release="0.2.0")
    assert env.model.endpoints_per_leaf == 2
    assert env.model.port_to_peer("leaf_1", "spine_1").name == "eth2"
    current = load_offline_net_env(scenario, "s")
    assert current.model.endpoints_per_leaf == 3
    assert current.model.port_to_peer("leaf_1", "spine_1").name == "eth3"


@pytest.mark.contract
@pytest.mark.parametrize(
    ("scenario", "failure", "capability"),
    [
        ("sdn_l3_clos", "mac_address_conflict", "mac-learning"),
        ("p4_dc_fabric", "mac_address_conflict", "mac-learning"),
        ("p4_dc_fabric", "host_ip_conflict", "arp-broadcast"),
    ],
)
def test_fault_targets_require_their_layer_two_semantics(scenario, failure, capability):
    from nika.workflows.benchmark.inject_resolve import validate_benchmark_case

    with pytest.raises(ValueError, match=capability):
        validate_benchmark_case(
            scenario,
            failure,
            {"host_name": "web_1", "host_name_2": "client_1_1"},
            "s",
        )
