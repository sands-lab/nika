"""Native IOS-XR analysis and a restricted concrete RouterOS IPv4 BGP model."""

from __future__ import annotations

import json
import re
import shlex
from ipaddress import IPv4Interface
from pathlib import Path

from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.kathara import startup_addresses, startup_static_routes
from nika.validation.batfish.snapshot import (
    prepare_snapshot,
    _cumulus_frr_snapshot,
    _edge,
)


def iosxr_batfish_config(config: str, shutdown=()) -> str:
    """Return a concrete IOS XR configuration for Batfish's native XR parser.

    ``shutdown`` names native interfaces administratively disabled in the
    analyzed operating state.
    """
    for interface in shutdown:
        statement = r"^interface " + re.escape(interface) + r"\n"
        if len(re.findall(statement, config, re.M)) != 1:
            raise ValueError("Unknown or ambiguous XR shutdown interface")
        config = re.sub(
            r"(^interface " + re.escape(interface) + r"\n)(.*?)(?=\n!|\Z)",
            lambda m: m[1] + " shutdown\n" + m[2].replace(" no shutdown", ""),
            config,
            flags=re.S | re.M,
        )
    # Rewrite equivalent statements into the forms Batfish's XR grammar parses:
    # one-line static routes and the Level-2 circuit type.
    config = re.sub(
        r"^( +)address-family (ipv4|ipv6) unicast (\S+ .+)$",
        lambda m: f"{m[1]}address-family {m[2]} unicast\n{m[1]} {m[3]}",
        config,
        flags=re.M,
    )
    config = re.sub(
        r"^( +)circuit-type level-2$",
        r"\1circuit-type level-2-only",
        config,
        flags=re.M,
    )
    return "!! IOS XR Configuration 7.0.0\n" + config


def vendor_configs(env):
    return {
        node: env.lab.machines[node].fs.readtext(env.BGP_CONFIG_PATH.lstrip("/"))
        for node in env.BGP_ROUTERS
    }


def vendor_addresses(config, device_profile):
    """Read concrete native IPv4 interface addresses, including loopbacks."""
    addresses = []
    if device_profile == "iosxr":
        interface = None
        for line in config.splitlines():
            if match := re.fullmatch(r"interface (\S+)", line):
                interface = match[1]
            elif line and not line[0].isspace():
                interface = None
            elif interface and line.lstrip().startswith("ipv4 address "):
                match = re.fullmatch(
                    r" ipv4 address ([0-9.]+(?:/[0-9]+| [0-9.]+))", line
                )
                if not match:
                    raise ValueError("Unsupported IOS XR IPv4 addressing")
                addresses.append(
                    (str(IPv4Interface(match[1].replace(" ", "/"))), interface)
                )
    elif device_profile == "routeros":
        for line in config.splitlines():
            parts = shlex.split(line)
            if parts[:2] != ["/ip", "address"]:
                continue
            if parts[:3] != ["/ip", "address", "add"]:
                raise ValueError("Unsupported RouterOS IPv4 addressing")
            attributes = [part.split("=", 1) for part in parts[3:]]
            if any(len(part) != 2 for part in attributes):
                raise ValueError("Unsupported RouterOS IPv4 addressing")
            values = dict(attributes)
            if len(attributes) != 2 or set(values) != {"address", "interface"}:
                raise ValueError("Unsupported RouterOS IPv4 addressing")
            addresses.append(
                (str(IPv4Interface(values["address"])), values["interface"])
            )
    else:
        raise ValueError(f"Unsupported native device profile: {device_profile}")
    return addresses


def vendor_ospf_interfaces(node, config, device_profile):
    """Read concrete IOS XR interface/area/passive bindings."""
    if device_profile != "iosxr":
        raise ValueError(f"Unsupported native device profile: {device_profile}")
    bindings = {}
    in_ospf, area, interface = False, None, None
    for line in config.splitlines():
        if re.fullmatch(r"router ospf \S+", line):
            if in_ospf:
                raise ValueError("Native ISP completion requires one OSPF instance")
            in_ospf, area, interface = True, None, None
        elif line and not line[0].isspace() and not line.startswith("!"):
            in_ospf = False
        elif in_ospf:
            if match := re.fullmatch(r" area ([0-9.]+)", line):
                area = (
                    int(IPv4Interface(f"{match[1]}/32").ip)
                    if "." in match[1]
                    else int(match[1])
                )
                interface = None
            elif match := re.fullmatch(r"  interface (\S+)", line):
                interface = match[1]
                if area is None or interface in bindings:
                    raise ValueError("Unsupported IOS XR OSPF interface binding")
                bindings[interface] = (area, False)
            elif line == "   passive enable" and interface:
                bindings[interface] = (area, True)
            elif line == "  !":
                interface = None
    return bindings


def build_vendor_snapshot(
    env, contract, root: Path, *, configs=None, shutdown=(), routing_model
):
    """Snapshot concrete IOS XR routers with their routed Linux hosts."""
    if routing_model not in {"iosxr-bgp", "iosxr-isp"}:
        raise ValueError("Unsupported native BGP platform")
    root = prepare_snapshot(root)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(exist_ok=True)
    configs = vendor_configs(env) if configs is None else configs
    if set(configs) != set(env.BGP_ROUTERS):
        raise ValueError("Candidate configurations must cover the declared routers")
    isp = routing_model.endswith("-isp")
    aliases = {}
    for node, config in configs.items():
        disabled = [env.BGP_INTERFACE_MAP[i] for n, i in shutdown if n == node]
        (root / "configs" / f"{node}.cfg").write_text(
            iosxr_batfish_config(config, disabled)
        )
        if isp:
            aliases[node] = {
                native_name: logical_name
                for logical_name, native_name in env.BGP_INTERFACE_MAP.items()
            }
    # Linux hosts are concrete routed endpoints with their original default routes.
    for node in set(env.lab.machines) - set(configs):
        startup = (
            env.lab.fs.readtext(f"{node}.startup")
            if env.lab.fs.exists(f"{node}.startup")
            else ""
        )
        addresses = startup_addresses(startup)
        if not addresses:
            continue
        lines = [f"hostname {node}"]
        for address, interface in addresses:
            lines += [f"interface {interface}", f" ip address {address}"]
            if (node, interface) in shutdown:
                lines += [" shutdown"]
            lines += ["!"]
        lines += [f"ip route {p} {g}" for p, g in startup_static_routes(startup)]
        (root / "configs" / f"{node}.cfg").write_text(
            _cumulus_frr_snapshot(
                node,
                "frr version 10.5\nfrr defaults traditional\n"
                + "\n".join(lines)
                + "\n",
            )
        )
    nodes = {p.stem for p in (root / "configs").glob("*.cfg")}
    edges = []
    for link in env.lab.links.values():
        endpoints = [
            (n, f"eth{i}")
            for n in link.machines
            if n in nodes
            for i, interface in env.lab.machines[n].interfaces.items()
            if interface.link.name == link.name
        ]
        if len(endpoints) == 2:
            endpoints = [
                (n, env.BGP_INTERFACE_MAP[i] if n in configs else i)
                for n, i in endpoints
            ]
            edges.append(_edge(*endpoints[0], *endpoints[1]))
    (root / "batfish" / "layer1_topology.json").write_text(json.dumps({"edges": edges}))
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=root,
        metadata={
            "routing_model": routing_model,
            "native_config_analysis": True,
            **({"interface_aliases": aliases} if isp else {}),
            "scope": "Native IOS-XR IPv4 OSPF/BGP configuration with routed Linux hosts"
            if isp
            else "Native IOS-XR IPv4 BGP configuration with routed Linux hosts",
        },
    )
