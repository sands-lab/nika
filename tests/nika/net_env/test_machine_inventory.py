from Kathara.model.Lab import Lab
import pytest

from nika.net_env.base import NetworkEnvBase
from nika.net_env.isp.kathara.lab import Isp
from nika.runtime.spec import MachineInventory, NodeIdentity, NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

pytestmark = pytest.mark.unit


def test_brief_lists_every_shared_segment_endpoint() -> None:
    env = NetworkEnvBase()
    env.lab = Lab("shared-segment")
    for name in ("controller", "worker1", "worker2", "client"):
        env.lab.new_machine(name, image="example/image")
        env.declare_machine(name, role=NodeRole.HOST)
        env.lab.connect_machine_to_link(name, "A")
    topology = env.get_info().split("Topology: ")[1]
    assert topology.count("(") == 1
    assert all(
        f"{name}:eth0" in topology
        for name in ("controller", "worker1", "worker2", "client")
    )


def test_machine_classification_uses_declared_identity_only() -> None:
    env = NetworkEnvBase()
    env.lab = Lab("explicit-identities")
    env.lab.new_machine("edge", image="vendor/unrelated-image")
    env.lab.new_machine("probe", image="vendor/another-image")
    env.declare_machine(
        "edge",
        role=NodeRole.ROUTER,
        capabilities=("frr", "linux"),
    )
    env.declare_machine(
        "probe",
        role=NodeRole.HOST,
        capabilities=("linux",),
        reachability_target=True,
    )

    env.load_machines()

    assert env.routers == ["edge"]
    assert env.hosts == ["probe"]
    assert env.machine_inventory.reachability_targets() == ["probe"]


def test_load_machines_rejects_undeclared_machine() -> None:
    env = NetworkEnvBase()
    env.lab = Lab("missing-identity")
    env.lab.new_machine("mystery", image=nika_image("base"))

    with pytest.raises(ValueError, match=r"missing=\['mystery'\]"):
        env.load_machines()


@pytest.mark.parametrize("backend", ["kathara", "containerlab"])
def test_audit_inventory_accepts_only_declared_deployment_infrastructure(
    backend,
) -> None:
    from types import SimpleNamespace

    from scripts.benchmark_audit.live import _restore_infrastructure_identities
    from nika.runtime.spec import LabSpec, NodeSpec
    from nika.workflows.benchmark.inject_resolve import _load_inventory

    env = NetworkEnvBase(backend=backend)
    env.declare_machine("edge", role=NodeRole.ROUTER)
    recorded = MachineInventory(
        {
            **env.machine_identities,
            "metrics": NodeIdentity(NodeRole.INFRASTRUCTURE, ("prometheus",)),
            "flow_probe": NodeIdentity(NodeRole.INFRASTRUCTURE, ("netflow",)),
            "remote_monitor": NodeIdentity(NodeRole.INFRASTRUCTURE),
            "unexpected_host": NodeIdentity(NodeRole.HOST),
        }
    )
    spec = LabSpec(
        "deployed",
        nodes=[
            NodeSpec(name, "example/image")
            for name in ("edge", "metrics", "flow_probe")
        ],
    )
    env.get_lab_spec = lambda: spec
    if backend == "kathara":
        env.lab = Lab("deployed")
        for node in spec.nodes:
            env.lab.new_machine(node.name, image=node.image)
    _restore_infrastructure_identities(
        env, SimpleNamespace(metadata={"machine_identities": recorded.to_dict()})
    )
    _load_inventory(env)
    assert env.routers == ["edge"]
    assert env.hosts == []
    assert "remote_monitor" not in env.machine_identities
    assert env.machine_identities["metrics"] == recorded.nodes["metrics"]
    assert env.machine_identities["flow_probe"] == recorded.nodes["flow_probe"]

    spec.nodes.append(NodeSpec("unexpected_host", "example/image"))
    if env.lab is not None:
        env.lab.new_machine("unexpected_host", image="example/image")
    with pytest.raises(ValueError, match=r"missing=\['unexpected_host'\]"):
        _load_inventory(env)
    spec.nodes.pop()
    spec.nodes = [node for node in spec.nodes if node.name != "edge"]
    if env.lab is not None:
        env.lab.machines.pop("unexpected_host")
        env.lab.machines.pop("edge")
    with pytest.raises(ValueError, match=r"extra=\['edge'\]"):
        _load_inventory(env)


def test_machine_inventory_round_trip() -> None:
    expected = MachineInventory(
        {
            "cache": NodeIdentity(
                role=NodeRole.INFRASTRUCTURE,
                capabilities=("rpki", "rtr"),
            )
        }
    )
    assert MachineInventory.from_dict(expected.to_dict()) == expected


def test_service_identity_requires_service_type() -> None:
    with pytest.raises(ValueError, match="requires service_type"):
        NodeIdentity(role=NodeRole.SERVICE)


def test_isp_routinator_is_infrastructure_not_reachability_target() -> None:
    env = Isp(topo="abilene", igp="ospf", bgp_mode="ebgp", rpki=True)
    env.load_machines()

    identity = env.machine_inventory.nodes["routinator"]
    assert identity.role is NodeRole.INFRASTRUCTURE
    assert {"rpki", "rtr"}.issubset(identity.capabilities)
    assert "routinator" not in env.machine_inventory.reachability_targets()


def test_dc_clos_declares_running_web_service() -> None:
    from nika.problems.rca.inventory import load_offline_net_env

    from nika.workflows.benchmark.inject_resolve import _load_inventory

    env = load_offline_net_env("dc_clos", "s", backend="containerlab")
    brief = env.get_info()
    assert "PCs: client_1_1" in brief
    assert "Web Servers: web_1" in brief
    _load_inventory(env)
    assert env.servers["web"] == ["web_1", "web_2", "web_3", "web_4"]
    assert env.target_roles()["hosts"] == env.hosts
    assert env.target_roles()["web"] == env.servers["web"]
