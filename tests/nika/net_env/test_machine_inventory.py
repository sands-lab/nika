from Kathara.model.Lab import Lab
import pytest

from nika.net_env.base import NetworkEnvBase
from nika.net_env.kathara.isp.isp.lab import Isp
from nika.runtime.spec import MachineInventory, NodeIdentity, NodeRole


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
    env.lab.new_machine("mystery", image="nika/base")

    with pytest.raises(ValueError, match=r"missing=\['mystery'\]"):
        env.load_machines()


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
