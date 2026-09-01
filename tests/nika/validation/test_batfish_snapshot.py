from __future__ import annotations

import json

from nika.net_env.isp.kathara.lab import Isp
from nika.topology import list_sndlib_topologies
from nika.validation.batfish.snapshot import build_isp_snapshot


def _environment(topology: str = "abilene") -> Isp:
    return Isp(topo=topology, igp="ospf", bgp_mode="none", lab_name="snapshot-test")


def test_snapshot_embeds_exact_deployment_configs_and_standard_supplemental_data(
    tmp_path,
) -> None:
    env = _environment()
    snapshot = build_isp_snapshot(
        root=tmp_path,
        contract=env.validation_contract,
        plan=env.plan,
        traffic=env.traffic,
        deployment_configs=env.deployment_configs,
    )
    for device, deployed in env.deployment_configs.items():
        snapshot_config = (snapshot.path / "configs" / f"{device}.cfg").read_text()
        assert snapshot_config.endswith(deployed)
    assert snapshot.snapshot_id == env.validation_contract.contract_id
    assert snapshot.metadata == {
        "contract_id": env.validation_contract.contract_id,
        "topology": env.plan.topology_name,
        "router_count": len(env.plan.nodes),
        "host_count": len(env.traffic.hosts),
        "layer1_edge_count": len(env.plan.links) + len(env.traffic.edge_links),
        "snapshot_config_format": "CUMULUS_CONCATENATED",
    }
    host = env.traffic.hosts[0]
    host_model = json.loads(
        (snapshot.path / "hosts" / f"{host.host_name}.json").read_text()
    )
    assert host_model["hostInterfaces"][host.host_iface]["gateway"] == host.gateway
    layer1 = json.loads(
        (snapshot.path / "batfish" / "layer1_topology.json").read_text()
    )
    assert len(layer1["edges"]) == len(env.plan.links) + len(env.traffic.edge_links)


def test_snapshot_is_deterministic_and_changes_with_topology(tmp_path) -> None:
    first = _environment("abilene")
    one = build_isp_snapshot(
        root=tmp_path / "one",
        contract=first.validation_contract,
        plan=first.plan,
        traffic=first.traffic,
        deployment_configs=first.deployment_configs,
    )
    two = build_isp_snapshot(
        root=tmp_path / "two",
        contract=first.validation_contract,
        plan=first.plan,
        traffic=first.traffic,
        deployment_configs=first.deployment_configs,
    )
    assert one.snapshot_id == two.snapshot_id

    second = _environment("polska")
    other = build_isp_snapshot(
        root=tmp_path / "other",
        contract=second.validation_contract,
        plan=second.plan,
        traffic=second.traffic,
        deployment_configs=second.deployment_configs,
    )
    assert other.snapshot_id != one.snapshot_id


def test_all_isp_catalog_topologies_have_unique_deterministic_intents() -> None:
    for topology in list_sndlib_topologies():
        first = _environment(topology).validation_contract
        second = _environment(topology).validation_contract
        assert first.to_json() == second.to_json()
        assert len({intent.id for intent in first.intents}) == len(first.intents)
