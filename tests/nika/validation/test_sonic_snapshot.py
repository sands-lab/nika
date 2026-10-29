"""SONiC deployments reach Batfish as their deployed CONFIG_DB and FRR files."""

from __future__ import annotations

import json

import pytest

from nika.net_env.contract import AdjacencyExpectation, ValidationIntent
from nika.net_env.isp.containerlab.lab import Isp
from nika.net_env.min3clos.lab import ContainerlabMin3Clos
from nika.net_env.utils.sonic.render import node_files
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.sonic import build_sonic_snapshot, design_frr
from nika.validation.batfish.verifier import BatfishVerifier
from nika.workflows.validation.static import _batfish_snapshot_builder

pytestmark = pytest.mark.contract


def _isp(igp: str, bgp_mode: str) -> Isp:
    return Isp(topo="abilene", igp=igp, bgp_mode=bgp_mode, device_profile="sonic")


def test_native_snapshot_holds_deployed_files_ports_and_outages(tmp_path):
    env = ContainerlabMin3Clos(device_profile="sonic")
    contract = env.get_validation_contract()
    snapshot = build_sonic_snapshot(
        env, contract, tmp_path, shutdown=[("leaf1", "eth1")]
    )

    for name, router in env.sonic_routers.items():
        folder = snapshot.path / "sonic_configs" / name
        deployed = node_files(router)
        assert (folder / "frr.conf").read_text() == deployed["frr.conf"]
        db = json.loads((folder / "config_db.json").read_text())
        expected = json.loads(deployed["config_db.json"])
        if name == "leaf1":
            assert db["PORT"]["Ethernet0"]["admin_status"] == "down"
            db["PORT"]["Ethernet0"]["admin_status"] = "up"
        assert db == expected
    edges = json.loads((snapshot.path / "batfish/layer1_topology.json").read_text())
    assert {
        (e["interface1"]["hostname"], e["interface1"]["interfaceName"])
        for e in edges["edges"]
    } == {("leaf1", "Ethernet0"), ("leaf2", "Ethernet0")}
    assert snapshot.metadata["native_config_analysis"] is True
    # min3clos runs IS-IS next to OSPF; Batfish's FRR model has no IS-IS.
    assert snapshot.metadata["unmodeled_protocols"] == ["isis"]
    assert snapshot.metadata["interface_aliases"]["leaf1"]["Ethernet0"] == "eth1"


def test_isis_only_design_uses_the_ios_xr_projection_of_the_same_design(tmp_path):
    env = _isp("isis", "ibgp_rr")
    snapshot = build_sonic_snapshot(env, env.get_validation_contract(), tmp_path)

    assert not (snapshot.path / "sonic_configs").exists()
    washng = (snapshot.path / "configs" / "washng.cfg").read_text()
    assert "router isis CORE" in washng
    assert "ipv4 address 10.0.0.7/31" in washng
    assert snapshot.metadata["native_config_analysis"] is False
    assert snapshot.metadata["interface_aliases"]["washng"] == {
        "Loopback0": "lo",
        "GigabitEthernet0/0/0/0": "eth0",
        "GigabitEthernet0/0/0/1": "eth1",
        "GigabitEthernet0/0/0/2": "eth2",
    }
    assert sorted(p.stem for p in (snapshot.path / "hosts").iterdir()) == sorted(
        h.host_name for h in env.traffic.hosts
    )


def test_design_frr_inverts_the_sonic_rendering():
    env = _isp("ospf", "ebgp")
    router = env.sonic_routers["atlang"]
    design = design_frr(router)

    assert "fpm address" not in design and "Ethernet" not in design
    assert "interface eth0\n ip address 10.0.0.1/31\n" in design
    # Router-level passive interfaces follow the SONiC port rename.
    assert " passive-interface Ethernet8" in router.frr_conf
    assert " passive-interface eth2" in design


def test_static_validation_dispatches_sonic_and_skips_sr_linux():
    sonic = _isp("ospf", "none")
    assert _batfish_snapshot_builder(sonic, sonic.get_validation_contract())
    srl = Isp(topo="abilene", igp="ospf", bgp_mode="none")
    assert _batfish_snapshot_builder(srl, srl.get_validation_contract()) is None


def test_unmodeled_protocol_adjacency_is_unsupported(tmp_path):
    env = ContainerlabMin3Clos(device_profile="sonic")
    intent = ValidationIntent(
        id="adj.isis.leaf1.spine",
        description="IS-IS adjacency.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="isis",
            local_node="leaf1",
            remote_node="spine",
            local_address="10.0.0.0",
            remote_address="10.0.0.1",
            isis_level=2,
            local_interface="eth1",
            remote_interface="eth1",
            local_router_id="30.0.0.1",
            remote_router_id="30.0.0.3",
        ),
    )
    contract = env.get_validation_contract().model_copy(update={"intents": (intent,)})

    class Client:
        def initialize(self, snapshot):
            return {}

        def execute(self, question):  # pragma: no cover - never reached
            raise AssertionError(question)

        def sanity_checks(self):
            return ()

    snapshot = ValidationSnapshot(
        snapshot_id="s",
        path=tmp_path,
        metadata={"routing_model": "sonic", "unmodeled_protocols": ["isis"]},
    )
    (result,) = BatfishVerifier(client=Client()).verify(contract, snapshot).results
    assert result.status == "unsupported"


def test_sonic_experiments_reject_unbound_capabilities(tmp_path):
    from nika.workflows.validation.scenario import validate_scenario_design
    from nika.workflows.validation.scenario_spec import ScenarioExperiment

    env = ContainerlabMin3Clos(device_profile="sonic")
    spec = ScenarioExperiment(
        scenario="min3clos",
        routing_model="sonic",
        deployment={"backend": "containerlab", "device_profile": "sonic"},
        healthy=env.get_validation_contract(),
        incidents=(),
    )
    with pytest.raises(ValueError, match="static analysis only"):
        validate_scenario_design(spec, tmp_path, runtime=True)
    srl = spec.model_copy(
        update={
            "deployment": spec.deployment.model_copy(
                update={"device_profile": "nokia_srlinux"}
            )
        }
    )
    with pytest.raises(ValueError, match="requires a SONiC deployment"):
        validate_scenario_design(srl, tmp_path)
    with pytest.raises(ValueError, match="no routing model"):
        ContainerlabMin3Clos(device_profile="sonic", routing_parameters={})
