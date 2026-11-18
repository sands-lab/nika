from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from nika.net_env.contract import ValidationContract
from nika.net_env.isp.igp.ifaces import plan_iface_name
from nika.net_env.isp.igp.plan import IspPlan
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.validation.base import ValidationSnapshot

SNAPSHOT_DIRNAME = "batfish-snapshot"
SNAPSHOT_METADATA_FILENAME = "batfish-snapshot-metadata.json"


def prepare_snapshot(root: str | Path) -> Path:
    """Replace this export's generated snapshot, including obsolete devices."""
    snapshot_root = Path(root) / SNAPSHOT_DIRNAME
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    snapshot_root.mkdir(parents=True)
    return snapshot_root


def build_isp_snapshot(
    *,
    root: str | Path,
    contract: ValidationContract,
    plan: IspPlan,
    traffic: IspTrafficAttachment,
    deployment_configs: dict[str, str],
) -> ValidationSnapshot:
    """Build a Batfish snapshot from the exact FRR deployment config strings."""
    snapshot_root = prepare_snapshot(root)
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


def normalize_frr_for_batfish(config: str) -> str:
    """Rewrite equivalent FRR statements into the forms Batfish parses."""
    # FRR accepts ``address-family ipv4`` as ``address-family ipv4 unicast``.
    config = re.sub(
        r"^( +)address-family ipv4$",
        r"\1address-family ipv4 unicast",
        config,
        flags=re.M,
    )
    # FRR accepts global maximum-paths as IPv4-unicast shorthand; Batfish
    # parses its explicit address-family form. Keep the numeric limit.
    config = re.sub(
        r"^ maximum-paths ([0-9]+)$",
        r" address-family ipv4 unicast\n  maximum-paths \1\n exit-address-family",
        config,
        flags=re.MULTILINE,
    )
    # FRR documents interface-level passive and router-level
    # passive-interface as equivalent. The pinned Batfish parser supports
    # only the latter. Normalize concrete commands, never the design plan.
    # https://docs.frrouting.org/en/stable-10.2/ospfd.html
    passive, interface = {}, None
    for line in config.splitlines():
        if match := re.fullmatch(r"interface (\S+)", line):
            interface = match[1]
        elif line.strip().startswith(("ip ospf passive", "no ip ospf passive")):
            if line.strip() not in {"ip ospf passive", "no ip ospf passive"}:
                raise ValueError("Address-specific OSPF passive syntax is not covered")
            if interface is None:
                raise ValueError("OSPF passive command outside an interface")
            passive[interface] = line.strip() == "ip ospf passive"
        elif line and not line[0].isspace() and not line.startswith(("!", "#")):
            interface = None
    if passive:
        if len(re.findall(r"^router ospf$", config, re.M)) != 1 or re.search(
            r"^\s+(?:no )?passive-interface\b", config, re.M
        ):
            raise ValueError(
                "Interface passive normalization requires one OSPF process without mixed passive syntax"
            )
        config = re.sub(r"^ (?:no )?ip ospf passive\n", "", config, flags=re.M)
        config += (
            "\nrouter ospf\n"
            + "".join(
                f" passive-interface {iface}\n"
                for iface, enabled in passive.items()
                if enabled
            )
            + "!\n"
        )
    return config


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


def rendered_router_config(name: str, config: str, device: str, shutdown=()) -> str:
    """Return a netlab-rendered FRR or IOS XR router for Batfish's own parser.

    ``shutdown`` names the rendered interfaces disabled in the analyzed state.
    """
    if device == "iosxr":
        from nika.validation.batfish.vendor_bgp import iosxr_batfish_config

        return iosxr_batfish_config(config, shutdown)
    if device != "frr":
        raise ValueError(f"Batfish has no parser for rendered device {device}")
    for interface in shutdown:
        # netlab emits one interface stanza per module; FRR merges them.
        blocks = list(
            re.finditer(
                rf"^interface {re.escape(interface)}\n(?:[ !][^\n]*\n)*", config, re.M
            )
        )
        if not blocks:
            raise ValueError(f"Unknown FRR shutdown interface {interface}")
        for index, block in reversed(list(enumerate(blocks))):
            header, _, body = block[0].partition("\n")
            body = body.replace(" no shutdown\n", "")
            if index == 0:
                body = " shutdown\n" + body
            config = (
                config[: block.start()] + f"{header}\n{body}" + config[block.end() :]
            )
    return _cumulus_frr_snapshot(
        name,
        "frr version 10.5\nfrr defaults traditional\n"
        + normalize_frr_for_batfish(config),
    )


def build_isp_render_snapshot(env, contract, root, *, configs, device, shutdown=()):
    """Snapshot an ISP design rendered by netlab for a parser Batfish supports.

    Routers come from the rendering; hosts, attachments and physical links come
    from the ISP plan, the same inputs netlab compiled.
    """
    from collections import defaultdict

    from nika.net_env.isp.iosxr import iosxr_interface

    plan, traffic = env.plan, env.traffic
    if set(configs) != {node.device_name for node in plan.nodes}:
        raise ValueError("The design rendering must cover every ISP router")

    def rendered(name: str) -> str:
        return iosxr_interface(name) if device == "iosxr" else name

    snapshot_root = prepare_snapshot(root)
    for directory in ("configs", "hosts", "batfish"):
        (snapshot_root / directory).mkdir(parents=True, exist_ok=True)
    disabled = defaultdict(list)
    for node, interface in shutdown:
        if node not in configs:
            raise ValueError(
                "Rendered ISP incidents may disable router interfaces only"
            )
        disabled[node].append(rendered(plan_iface_name(interface)))
    for node, config in sorted(configs.items()):
        (snapshot_root / "configs" / f"{node}.cfg").write_text(
            rendered_router_config(node, config, device, disabled[node])
        )
    for host in sorted(traffic.hosts, key=lambda item: item.host_name):
        _write_json(
            snapshot_root / "hosts" / f"{host.host_name}.json",
            {
                "hostname": host.host_name,
                "hostInterfaces": {
                    host.host_iface: {
                        "name": host.host_iface,
                        "prefix": f"{host.address}/{host.prefixlen}",
                        "gateway": host.gateway,
                    }
                },
            },
        )
    edges = [
        _edge(
            link.endpoint_a,
            rendered(link.iface_a),
            link.endpoint_b,
            rendered(link.iface_b),
        )
        for link in sorted(plan.links, key=lambda item: item.link_id)
    ]
    edges += [
        _edge(
            edge.router_device,
            rendered(plan_iface_name(edge.router_iface)),
            edge.host_name,
            "eth0",
        )
        for edge in sorted(traffic.edge_links, key=lambda item: item.router_device)
    ]
    _write_json(snapshot_root / "batfish" / "layer1_topology.json", {"edges": edges})
    metadata = {
        "routing_model": f"{env.device_profile}-isp",
        "native_config_analysis": False,
        "design_rendering": device,
        "scope": "The ISP routing design rendered by netlab as "
        + ("IOS XR" if device == "iosxr" else "FRR")
        + "; native configuration syntax is checked only by native execution",
    }
    if device == "iosxr":
        metadata["interface_aliases"] = {
            node.device_name: {rendered(i.name): i.name for i in node.interfaces}
            for node in plan.nodes
        }
    return ValidationSnapshot(
        snapshot_id=contract.contract_id, path=snapshot_root, metadata=metadata
    )


def build_lab_render_snapshot(env, contract, root, *, configs, shutdown=()):
    """Snapshot a small BGP design rendered by netlab as FRR.

    Routers come from the rendering under their ``ethN`` names. Kathara labs
    add their Linux hosts from the concrete startup commands.
    """
    from collections import defaultdict

    from nika.validation.batfish.kathara import (
        startup_addresses,
        startup_static_routes,
    )

    snapshot_root = prepare_snapshot(root)
    for directory in ("configs", "batfish"):
        (snapshot_root / directory).mkdir(parents=True, exist_ok=True)
    disabled = defaultdict(list)
    for node, interface in shutdown:
        disabled[node].append(plan_iface_name(interface))
    for node, config in sorted(configs.items()):
        (snapshot_root / "configs" / f"{node}.cfg").write_text(
            rendered_router_config(node, config, "frr", disabled[node])
        )
    edges = []
    if env.backend == "kathara":
        for node in sorted(set(env.lab.machines) - set(configs)):
            path = f"{node}.startup"
            startup = env.lab.fs.readtext(path) if env.lab.fs.exists(path) else ""
            addresses = startup_addresses(startup)
            if not addresses:
                continue
            lines = [f"hostname {node}"]
            for address, interface in addresses:
                lines += [f"interface {interface}", f" ip address {address}"]
                if interface in disabled[node]:
                    lines.append(" shutdown")
                lines.append("!")
            lines += [f"ip route {p} {g}" for p, g in startup_static_routes(startup)]
            (snapshot_root / "configs" / f"{node}.cfg").write_text(
                _cumulus_frr_snapshot(
                    node,
                    "frr version 10.5\nfrr defaults traditional\n"
                    + "\n".join(lines)
                    + "\n",
                )
            )
        nodes = {p.stem for p in (snapshot_root / "configs").glob("*.cfg")}
        for link in env.lab.links.values():
            endpoints = [
                (n, f"eth{i}")
                for n in link.machines
                if n in nodes
                for i, interface in env.lab.machines[n].interfaces.items()
                if interface.link.name == link.name
            ]
            if len(endpoints) == 2:
                edges.append(_edge(*endpoints[0], *endpoints[1]))
    else:
        for link in env.get_lab_spec().links:
            a, b = (endpoint.split(":") for endpoint in link.endpoints)
            if a[0] in configs and b[0] in configs:
                edges.append(
                    _edge(a[0], plan_iface_name(a[1]), b[0], plan_iface_name(b[1]))
                )
    _write_json(snapshot_root / "batfish" / "layer1_topology.json", {"edges": edges})
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=snapshot_root,
        metadata={
            "native_config_analysis": False,
            "design_rendering": "frr",
            "scope": "The BGP design rendered by netlab as FRR; native "
            "configuration syntax is checked only by native execution",
        },
    )
