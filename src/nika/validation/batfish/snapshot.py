from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nika.net_env.contract import ValidationContract
from nika.net_env.isp.igp.plan import IspPlan
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.validation.base import ValidationSnapshot

SNAPSHOT_DIRNAME = "batfish-snapshot"
SNAPSHOT_METADATA_FILENAME = "batfish-snapshot-metadata.json"


def build_isp_snapshot(
    *,
    root: str | Path,
    contract: ValidationContract,
    plan: IspPlan,
    traffic: IspTrafficAttachment,
    deployment_configs: dict[str, str],
) -> ValidationSnapshot:
    """Build a Batfish snapshot from the exact FRR deployment config strings."""
    snapshot_root = Path(root) / SNAPSHOT_DIRNAME
    configs_dir = snapshot_root / "configs"
    hosts_dir = snapshot_root / "hosts"
    batfish_dir = snapshot_root / "batfish"
    for directory in (configs_dir, hosts_dir, batfish_dir):
        directory.mkdir(parents=True, exist_ok=True)

    expected_devices = {node.device_name for node in plan.nodes}
    if set(deployment_configs) != expected_devices:
        raise ValueError(
            "deployment config devices do not match ISP plan: "
            f"missing={sorted(expected_devices - set(deployment_configs))}, "
            f"unknown={sorted(set(deployment_configs) - expected_devices)}"
        )

    for device in sorted(deployment_configs):
        content = deployment_configs[device]
        snapshot_content = _cumulus_frr_snapshot(device, content)
        (configs_dir / f"{device}.cfg").write_text(snapshot_content, encoding="utf-8")

    for host in sorted(traffic.hosts, key=lambda item: item.host_name):
        payload = {
            "hostname": host.host_name,
            "hostInterfaces": {
                host.host_iface: {
                    "name": host.host_iface,
                    "prefix": f"{host.address}/{host.prefixlen}",
                    "gateway": host.gateway,
                }
            },
        }
        _write_json(hosts_dir / f"{host.host_name}.json", payload)

    edges: list[dict[str, Any]] = []
    for link in sorted(plan.links, key=lambda item: item.link_id):
        edges.append(
            _edge(link.endpoint_a, link.iface_a, link.endpoint_b, link.iface_b)
        )
    for edge in sorted(traffic.edge_links, key=lambda item: item.router_device):
        edges.append(
            _edge(
                edge.router_device,
                edge.router_iface,
                edge.host_name,
                "eth0",
            )
        )
    _write_json(batfish_dir / "layer1_topology.json", {"edges": edges})

    snapshot_id = contract.contract_id
    metadata = {
        "contract_id": contract.contract_id,
        "topology": plan.topology_name,
        "router_count": len(plan.nodes),
        "host_count": len(traffic.hosts),
        "layer1_edge_count": len(edges),
        "snapshot_config_format": "CUMULUS_CONCATENATED",
    }
    _write_json(Path(root) / SNAPSHOT_METADATA_FILENAME, metadata)
    return ValidationSnapshot(
        snapshot_id=snapshot_id,
        path=snapshot_root,
        metadata=metadata,
    )


def _edge(node1: str, interface1: str, node2: str, interface2: str) -> dict[str, Any]:
    return {
        "interface1": {"hostname": node1, "interfaceName": interface1},
        "interface2": {"hostname": node2, "interfaceName": interface2},
    }


def _cumulus_frr_snapshot(device: str, frr_conf: str) -> str:
    """Wrap exact FRR bytes in Batfish's standard concatenated input format."""
    return (
        f"{device}\n"
        "# This file describes the network interfaces\n"
        "auto lo\n"
        "iface lo inet loopback\n"
        "# ports.conf --\n"
        f"{frr_conf}"
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
