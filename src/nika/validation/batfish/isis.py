"""Restricted FRR IS-IS projection into native IOS XR configuration."""

from __future__ import annotations

import json
import re
from ipaddress import IPv4Interface
from pathlib import Path

from nika.net_env.isp.igp.frr import ISIS_PROCESS
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.snapshot import prepare_snapshot, _edge
from nika.validation.batfish.frr_ibgp import project_frr_ibgp, split_frr_ibgp


_IFACE_RE = re.compile(r"(?:lo|eth[0-9]+)\Z")
_METRIC_RE = re.compile(r"isis metric ([0-9]+)\Z")
_ADDRESS_RE = re.compile(r"ip address ([0-9.]+/[0-9]+)\Z")
_NET_RE = re.compile(
    r"(?:[0-9A-Fa-f]{2}|[0-9A-Fa-f]{4})(?:\.(?:[0-9A-Fa-f]{2}|[0-9A-Fa-f]{4}))+\Z"
)


def parse_frr_isis(node: str, config: str) -> tuple[str, dict[str, dict]]:
    """Parse the generated L2-only, wide-metric FRR IPv4 IS-IS subset.

    Interface addresses may be absent in a syntactically valid candidate so
    callers can validate withdrawal mutations against the original sketch.
    The returned metric for loopback is FRR's default IS-IS metric (10).
    """
    net = None
    router_values: dict[str, str] = {}
    interfaces: dict[str, dict] = {}
    section = "global"
    current_iface = None
    seen_global: set[str] = set()

    def reject(line: str) -> None:
        raise ValueError(f"Unsupported FRR IS-IS config on {node}: {line!r}")

    for raw in config.splitlines():
        line = raw.strip()
        if not line or line.startswith("!"):
            continue
        if line.startswith("interface "):
            name = line.removeprefix("interface ")
            if not _IFACE_RE.fullmatch(name) or name in interfaces:
                reject(line)
            current_iface = name
            interfaces[name] = {
                "address": None,
                "metric": 10,
                "passive": False,
                "isis_enabled": False,
                "circuit_type": None,
                "metric_explicit": False,
            }
            section = "interface"
            continue
        if line == f"router isis {ISIS_PROCESS}":
            if section == "router" or "router" in seen_global:
                reject(line)
            seen_global.add("router")
            section = "router"
            current_iface = None
            continue
        if line == "log file /var/log/frr/frr.log":
            if line in seen_global:
                reject(line)
            seen_global.add(line)
            continue
        if section == "global":
            if line in {
                "frr version 10.5.3",
                "frr defaults traditional",
                "log file /var/log/frr/frr.log",
            }:
                if line in seen_global:
                    reject(line)
                seen_global.add(line)
                continue
            if line.startswith("hostname "):
                if "hostname" in seen_global or line != f"hostname {node}":
                    reject(line)
                seen_global.add("hostname")
                continue
            reject(line)
        if section == "interface":
            iface = interfaces[current_iface]
            match = _ADDRESS_RE.fullmatch(line)
            if match:
                if iface["address"] is not None:
                    reject(line)
                try:
                    iface["address"] = IPv4Interface(match.group(1))
                except ValueError:
                    reject(line)
            elif line == f"ip router isis {ISIS_PROCESS}":
                if iface["isis_enabled"]:
                    reject(line)
                iface["isis_enabled"] = True
            elif line == "isis circuit-type level-2-only":
                if iface["circuit_type"] is not None:
                    reject(line)
                iface["circuit_type"] = "level-2-only"
            elif line == "isis passive":
                if iface["passive"]:
                    reject(line)
                iface["passive"] = True
            else:
                match = _METRIC_RE.fullmatch(line)
                if not match or iface["metric_explicit"]:
                    reject(line)
                metric = int(match.group(1))
                if not 1 <= metric <= 16_777_214:
                    reject(line)
                iface["metric"] = metric
                iface["metric_explicit"] = True
            continue
        if section == "router":
            if line.startswith("net "):
                if net is not None:
                    reject(line)
                candidate = line.removeprefix("net ")
                if not _NET_RE.fullmatch(candidate):
                    reject(line)
                octets = bytes.fromhex(candidate.replace(".", ""))
                if len(octets) < 8 or len(octets) > 20 or octets[-1] != 0:
                    reject(line)
                net = candidate
                router_values["net"] = candidate
            elif line in {"is-type level-2-only", "metric-style wide"}:
                key, value = line.split(" ", 1)
                if key in router_values:
                    reject(line)
                router_values[key] = value
            else:
                reject(line)

    if (
        net is None
        or router_values.get("is-type") != "level-2-only"
        or router_values.get("metric-style") != "wide"
    ):
        raise ValueError(f"Incomplete FRR IS-IS configuration on {node}")
    if (
        not {"frr version 10.5.3", "frr defaults traditional", "hostname"}
        <= seen_global
    ):
        raise ValueError(f"Incomplete FRR configuration header on {node}")
    if not interfaces:
        raise ValueError(f"FRR IS-IS config on {node} has no interfaces")
    for name, iface in interfaces.items():
        if not iface["isis_enabled"]:
            if (
                iface["passive"]
                or iface["circuit_type"] is not None
                or iface["metric_explicit"]
            ):
                raise ValueError(
                    f"Incomplete disabled FRR IS-IS interface on {node}:{name}"
                )
        elif name == "lo":
            if (
                not iface["passive"]
                or iface["circuit_type"] is not None
                or iface["metric_explicit"]
            ):
                raise ValueError(f"Unsupported FRR IS-IS loopback config on {node}")
        elif iface["circuit_type"] != "level-2-only" or not iface["metric_explicit"]:
            raise ValueError(f"Incomplete FRR IS-IS interface config on {node}:{name}")
        iface["enabled"] = iface["isis_enabled"]
        del iface["isis_enabled"]
        del iface["circuit_type"]
        del iface["metric_explicit"]
    if "lo" not in interfaces:
        raise ValueError(f"FRR IS-IS config on {node} has no loopback")
    return net, interfaces


def project_frr_isis(node: str, config: str, *, shutdown=()) -> str:
    """Translate one supported FRR config to native IOS XR IS-IS syntax."""
    igp_config, bgp_fragment = split_frr_ibgp(config)
    net, interfaces = parse_frr_isis(node, igp_config)
    lines = [f"hostname {node}"]
    for original, details in interfaces.items():
        native = _native_interface(original)
        lines.append(f"interface {native}")
        if details["address"] is not None:
            lines.append(f" ipv4 address {details['address']}")
        if (node, original) in shutdown:
            lines.append(" shutdown")
        lines.append("!")
    lines.extend(
        [
            f"router isis {ISIS_PROCESS}",
            f" net {net}",
            " is-type level-2-only",
            " address-family ipv4 unicast",
            "  metric-style wide",
            " !",
        ]
    )
    for original, details in interfaces.items():
        if not details["enabled"]:
            continue
        lines.extend([f" interface {_native_interface(original)}"])
        if original != "lo":
            lines.append("  circuit-type level-2-only")
        if details["passive"]:
            lines.append("  passive")
        lines.extend(
            [
                "  address-family ipv4 unicast",
                f"   metric {details['metric']} level 2",
                "  !",
                " !",
            ]
        )
    lines.append("!")
    return "\n".join(lines) + "\n" + project_frr_ibgp(bgp_fragment)


def build_isis_snapshot(
    env,
    contract,
    root: str | Path,
    *,
    configs=None,
    shutdown=(),
    routing_model="frr-isis",
) -> ValidationSnapshot:
    """Build an IOS XR IS-IS Batfish model from concrete FRR configs."""
    if routing_model != "frr-isis":
        raise ValueError("Unsupported IS-IS routing projection")
    plan = env.plan
    if (
        plan.igp != "isis"
        or (env.bgp_plan is not None and env.bgp_plan.mode != "ibgp_rr")
        or env.rpki
        or env.rtbh
    ):
        raise ValueError(
            "IS-IS projection requires an ordinary no-BGP or iBGP RR ISP profile"
        )
    deployment_configs = env.deployment_configs if configs is None else configs
    expected = {node.device_name for node in plan.nodes}
    if set(deployment_configs) != expected:
        raise ValueError(
            "deployment config devices do not match ISP plan: "
            f"missing={sorted(expected - set(deployment_configs))}, "
            f"unknown={sorted(set(deployment_configs) - expected)}"
        )
    snapshot_root = prepare_snapshot(root)
    configs_dir = snapshot_root / "configs"
    batfish_dir = snapshot_root / "batfish"
    configs_dir.mkdir(parents=True, exist_ok=True)
    batfish_dir.mkdir(parents=True, exist_ok=True)
    aliases = {}
    plan_nodes = {node.device_name: node for node in plan.nodes}
    for node, config in sorted(deployment_configs.items()):
        igp_config, _ = split_frr_ibgp(config)
        _, interfaces = parse_frr_isis(node, igp_config)
        _validate_startup_addresses(node, plan_nodes[node].startup_commands, interfaces)
        native_map = {_native_interface(name): name for name in interfaces}
        aliases[node] = native_map
        projected = project_frr_isis(node, config, shutdown=shutdown)
        (configs_dir / f"{node}.cfg").write_text(projected, encoding="utf-8")

    edges = []
    for link in sorted(plan.links, key=lambda item: item.link_id):
        edges.append(
            _edge(
                link.endpoint_a,
                _native_interface(link.iface_a),
                link.endpoint_b,
                _native_interface(link.iface_b),
            )
        )
    (batfish_dir / "layer1_topology.json").write_text(
        json.dumps({"edges": edges}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "contract_id": contract.contract_id,
        "topology": plan.topology_name,
        "router_count": len(plan.nodes),
        "layer1_edge_count": len(edges),
        "routing_model": routing_model,
        "native_config_analysis": False,
        "interface_aliases": aliases,
        "scope": "FRR IPv4 IS-IS L2-only wide metrics and optional ordinary iBGP RR projected to IOS XR; connected router interfaces only",
    }
    Path(root).mkdir(parents=True, exist_ok=True)
    (Path(root) / "batfish-snapshot-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=snapshot_root,
        metadata=metadata,
    )


def _native_interface(original: str) -> str:
    if original == "lo":
        return "Loopback0"
    match = re.fullmatch(r"eth([0-9]+)", original)
    if match is None:
        raise ValueError(f"Unsupported FRR interface name {original!r}")
    return f"GigabitEthernet0/0/0/{match.group(1)}"


def _validate_startup_addresses(node, commands, interfaces) -> None:
    """Reject Linux route/host startup behavior outside the projected config."""
    expected = {
        (str(details["address"]), name)
        for name, details in interfaces.items()
        if details["address"] is not None
    }
    found = set()
    allowed_boot = {
        "service frr start",
        "bash -c 'for i in 1 2 3 4 5 6 7 8 9 10; do vtysh -c \"show version\" >/dev/null 2>&1 && break; sleep 1; done'",
        "vtysh -f /etc/frr/frr.conf",
    }
    for command in commands:
        match = re.fullmatch(
            r"ip addr add ([0-9.]+/[0-9]+) dev (lo|eth[0-9]+)", command
        )
        if match:
            try:
                address = str(IPv4Interface(match.group(1)))
            except ValueError as exc:
                raise ValueError(
                    f"Unsupported startup address on {node}: {command}"
                ) from exc
            found.add((address, match.group(2)))
        elif command not in allowed_boot:
            raise ValueError(
                f"Unsupported ISP router startup command on {node}: {command}"
            )
    if found != expected:
        raise ValueError(
            f"FRR IS-IS interface addresses on {node} do not match router startup: "
            f"missing={sorted(expected - found)}, unknown={sorted(found - expected)}"
        )
