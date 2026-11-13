"""Size inventories, fault targets, and vendor deployment contracts."""

from unittest.mock import patch

import pytest
import yaml

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.rca.materialize import ground_truth_for_case
from nika.problems.support.benchmark_targets import attached_end_hosts
from nika.problems.link_interface.link import LinkFailureParams
from nika.validation.symptom.probe import _resolve_path
from nika.workflows.benchmark.task_label import format_task_label, parse_task_label

pytestmark = pytest.mark.contract


def environment(scenario, **kwargs):
    with patch("Kathara.manager.Kathara.Kathara.get_instance", return_value=object()):
        return get_net_env_instance(scenario, **kwargs)


@pytest.mark.parametrize("scenario,default", [("k8s_lab", "m"), ("llmd_lab", "m")])
def test_omitted_size_preserves_published_network_and_task_label(scenario, default):
    env = environment(scenario)
    assert env.topo_size == default
    assert set(env.kubernetes_nodes) == {
        "controller",
        *(f"worker{i}" for i in range(1, 6)),
    }
    label = format_task_label(scenario, "link_down")
    assert label == f"{scenario}_link_down"
    assert parse_task_label(label) == (scenario, "", "link_down")


@pytest.mark.parametrize("scenario", ["k8s_lab", "llmd_lab"])
def test_unknown_size_is_rejected(scenario):
    with pytest.raises(ValueError, match="Topology size"):
        environment(scenario, topo_size="xl")


@pytest.mark.parametrize("scenario", ["k8s_lab", "llmd_lab"])
@pytest.mark.parametrize("size,workers", [("s", 3), ("m", 5), ("l", 8)])
def test_kubernetes_inventory_storage_and_addresses_scale_together(
    scenario, size, workers
):
    env = environment(scenario, topo_size=size)
    nodes = {"controller", *(f"worker{i}" for i in range(1, workers + 1))}
    assert set(env.kubernetes_nodes) == nodes
    assert set(env.target_roles()["k8s_nodes"]) == nodes
    addresses = env.topology.addresses
    assert len(set(addresses.values())) == len(nodes)
    for node, address in addresses.items():
        machine = env.lab.machines[node]
        hosts = dict(
            line.split() for line in machine.fs.readtext("/etc/hosts").splitlines()
        )
        assert hosts[address] == node
        assert f"ip address add {address}/24 dev eth0" in env.lab.fs.readtext(
            f"{node}.startup"
        )
    if scenario == "k8s_lab":
        for leaf, attached in env.topology.leaf_nodes.items():
            assert set(attached_end_hosts(env, leaf)) == set(attached)
        controller = env.lab.machines["controller"]
        volumes = list(yaml.safe_load_all(controller.fs.readtext("/k8s/pv.yaml")))
        pins = {
            v["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0][
                "matchExpressions"
            ][0]["values"][0]
            for v in volumes
        }
        assert pins == set(env.kubernetes_storage_nodes)
        assert pins <= nodes - {"controller"}
        peers = list(
            yaml.safe_load_all(controller.fs.readtext("/k8s/metallb-conf.yaml"))
        )[:2]
        selected = {
            n
            for peer in peers
            for n in peer["spec"]["nodeSelectors"][0]["matchExpressions"][0]["values"]
        }
        assert selected == nodes - {"controller"}
    else:
        assert not {"200.0.0.7", "200.0.0.8", "200.0.0.240"} & set(addresses.values())
    truth = ground_truth_for_case(
        problem="k8s_worker_apiserver_partition",
        params={"control_node": "controller", "node_name": f"worker{workers}"},
        scenario=scenario,
        topo_size=size,
        net_env=env,
    )
    assert truth.root_causes[0].resource.node == f"worker{workers}"


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_min3clos_rejects_scaling(size):
    from nika.net_env.net_env_pool import resolve_scenario_topo_size

    with pytest.raises(ValueError, match="does not use topology sizes"):
        resolve_scenario_topo_size("min3clos", size)


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_kubernetes_hijack_has_a_remote_observer_and_live_destination(size):
    env = environment("k8s_lab", topo_size=size)
    for target in env.bgp_hijack_targets():
        assert target.observer != target.hijacker
        assert target.observer_host in env.topology.leaf_nodes[target.observer]
        victim = next(
            node
            for node, address in env.topology.addresses.items()
            if address == target.probe_ip
        )
        assert victim != target.observer_host
        assert victim in env.topology.leaf_nodes[target.observer]
        assert victim not in env.topology.leaf_nodes[target.hijacker]


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_kubernetes_access_link_probe_targets_the_attached_worker(size):
    env = environment("k8s_lab", topo_size=size)
    workers = env.topology.leaf_nodes["leaf_1_2"]
    for node, interface in (
        ("leaf_1_2", f"eth{len(workers) + 1}"),
        (workers[-1], "eth0"),
    ):
        path = _resolve_path(
            "k8s_lab",
            LinkFailureParams(host_name=node, intf_name=interface),
            topo_size=size,
            net_env=env,
        )
        assert path.src_host == "client"
        assert path.dst_ip == env.topology.addresses[workers[-1]]


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_inference_access_link_probe_targets_the_affected_node(size):
    env = environment("llmd_lab", topo_size=size)
    for node in ("controller", list(env.topology.addresses)[-1]):
        path = _resolve_path(
            "llmd_lab",
            LinkFailureParams(host_name=node, intf_name="eth0"),
            topo_size=size,
            net_env=env,
        )
        assert path.src_host == "client"
        assert path.dst_ip == env.topology.addresses[node]


@pytest.mark.parametrize("profile", ["nokia_srlinux", "sonic"])
def test_min3clos_keeps_fixed_fabric(profile):
    env = environment("min3clos", device_profile=profile)
    spec = env.get_lab_spec()
    assert set(env.routers) == {"leaf1", "leaf2", "spine"}
    assert len(spec.nodes) == 5
    assert len(spec.links) == 4
    assert len(env.validation_contract.intents) == 8


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("profile", ["nokia_srlinux", "sonic"])
def test_vendor_dc_clos_preserves_design_services_and_routing(size, profile):
    reference = environment("dc_clos", topo_size=size)
    env = environment(
        "dc_clos", backend="containerlab", topo_size=size, device_profile=profile
    )
    assert set(env.routers) == set(reference.routers)
    assert {n.name for n in env.get_lab_spec().nodes} == set(reference.lab.machines)
    assert len(env.get_topology()) == len(reference.get_topology())
    assert env.bgp_neighbors == reference.bgp_neighbors
    assert env.bgp_origin_prefixes() == reference.bgp_origin_prefixes()
    assert env.validation_contract == reference.validation_contract
    assert env.client_addresses == reference.client_addresses
    assert env.dns_records == reference.dns_records
    assert env.business_workload().processes
    brief = env.get_info()
    platform = "SONiC" if env.sonic else "Nokia SR Linux"
    assert f"Routers ({platform}):" in brief
    assert "Dns Servers: web_1" in brief
    assert "Topology:" in brief
