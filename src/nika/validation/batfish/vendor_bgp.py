"""Native IOS-XR analysis and a restricted concrete RouterOS IPv4 BGP model."""

from __future__ import annotations

import json
import re
import shlex
from ipaddress import IPv4Interface
from pathlib import Path

from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.kathara import startup_addresses, startup_static_routes
from nika.validation.batfish.routeros_isp import (
    project_routeros_isp,
    routeros_model_interface,
)
from nika.validation.batfish.snapshot import (
    prepare_snapshot,
    _cumulus_frr_snapshot,
    _edge,
)


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
    """Read concrete interface/area/passive bindings for native ISP completion."""
    if device_profile == "routeros":
        projected = project_routeros_isp(node, config)
        addresses = vendor_addresses(config, device_profile)
        declarations = re.findall(
            r"^ network ([0-9./]+) area ([0-9.]+)$", projected, re.M
        )
        passive = set(re.findall(r"^ passive-interface (\S+)$", projected, re.M))
        return {
            interface: (
                int(IPv4Interface(f"{area}/32").ip),
                routeros_model_interface(interface) in passive,
            )
            for address, interface in addresses
            for network, area in declarations
            if IPv4Interface(address).ip in IPv4Interface(network).network
        }
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


def project_routeros_bgp(node, config, *, shutdown=()):
    """Accept only the addressed direct-eBGP and exact-prefix-filter subset.

    Parse the concrete candidate, including mutations, rather than regenerating
    an idealized configuration from the original requirements or solver result.
    """
    addresses, instances, connections, filters = [], [], [], {}
    for line in config.splitlines():
        if not line.strip():
            continue
        parts = shlex.split(line)
        values = dict(part.split("=", 1) for part in parts[4:] if "=" in part)
        if parts[:3] == ["/system", "identity", "set"]:
            if parts != ["/system", "identity", "set", f"name={node}"]:
                raise ValueError("Unsupported RouterOS identity")
        elif parts[:3] == ["/ip", "address", "add"]:
            values = dict(part.split("=", 1) for part in parts[3:])
            if set(values) != {"address", "interface"}:
                raise ValueError("Unsupported RouterOS addressing")
            addresses.append(values)
        elif parts[:4] == ["/routing", "bgp", "instance", "add"]:
            if set(values) != {"name", "as", "router-id"}:
                raise ValueError("Unsupported RouterOS BGP instance")
            instances.append(values)
        elif parts[:4] == ["/routing", "bgp", "connection", "add"]:
            if (
                set(values)
                - {
                    "name",
                    "instance",
                    "local.role",
                    "local.address",
                    "remote.address",
                    "remote.as",
                    "output.redistribute",
                    "output.filter-chain",
                }
                or values.get("local.role") != "ebgp"
                or values.get("output.redistribute") != "connected"
            ):
                raise ValueError("Unsupported RouterOS BGP connection")
            connections.append(values)
        elif parts[:4] == ["/routing", "filter", "rule", "add"]:
            if set(values) != {"chain", "rule"}:
                raise ValueError("Unsupported RouterOS filter attributes")
            filters.setdefault(values["chain"], []).append(values["rule"])
        else:
            raise ValueError(f"Unsupported RouterOS statement: {line}")
    if len(instances) != 1 or not addresses or not connections:
        raise ValueError("RouterOS projection requires one instance and direct peers")
    instance = instances[0]
    lines = [f"hostname {node}"]
    for address in addresses:
        lines += [
            f"interface {address['interface']}",
            f" ip address {address['address']}",
        ]
        if (node, address["interface"]) in shutdown:
            lines += [" shutdown"]
        lines += ["!"]
    lines += [
        f"router bgp {instance['as']}",
        f" bgp router-id {instance['router-id']}",
        " no bgp ebgp-requires-policy",
    ]
    # output.redistribute=connected originates every addressed connected network;
    # the concrete export chain determines which reaches a peer.
    lines += [f" network {IPv4Interface(a['address']).network}" for a in addresses]
    policies = []
    for index, connection in enumerate(connections):
        if connection["instance"] != instance["name"] or connection[
            "local.address"
        ] not in {str(IPv4Interface(a["address"]).ip) for a in addresses}:
            raise ValueError("Unsupported RouterOS connection binding")
        peer = connection["remote.address"]
        chain = connection.get("output.filter-chain")
        if chain not in filters or filters[chain][-1] != "reject;":
            raise ValueError("RouterOS exports require an explicit terminating reject")
        lines += [
            f" neighbor {peer} remote-as {connection['remote.as']}",
            f" neighbor {peer} route-map NC-{index} out",
        ]
        for seq, rule in enumerate(filters[chain][:-1], 1):
            match = re.fullmatch(
                r"if \(dst == ([0-9./]+) && dst-len == ([0-9]+)\) \{ (accept|reject); \}",
                rule,
            )
            if not match:
                raise ValueError("Only exact-prefix RouterOS export rules are modeled")
            prefix, length, action = match.groups()
            network = IPv4Interface(prefix).network
            if int(length) != network.prefixlen or str(network) != prefix:
                raise ValueError("RouterOS exact prefix and length disagree")
            name = f"NC-{index}-{seq}"
            policies += [
                f"ip prefix-list {name} seq 5 permit {prefix}",
                f"route-map NC-{index} {'permit' if action == 'accept' else 'deny'} {seq * 10}",
                f" match ip address prefix-list {name}",
                "!",
            ]
    return "\n".join([*lines, "!", *policies]) + "\n"


def build_vendor_snapshot(
    env, contract, root: Path, *, configs=None, shutdown=(), routing_model
):
    if routing_model not in {"iosxr-bgp", "routeros-bgp", "iosxr-isp", "routeros-isp"}:
        raise ValueError("Unsupported native BGP platform")
    root = prepare_snapshot(root)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(exist_ok=True)
    configs = vendor_configs(env) if configs is None else configs
    if set(configs) != set(env.BGP_ROUTERS):
        raise ValueError("Candidate configurations must cover the declared routers")
    native = routing_model in {"iosxr-bgp", "iosxr-isp"}
    isp = routing_model.endswith("-isp")
    aliases = {}
    for node, config in configs.items():
        disabled = [(n, env.BGP_INTERFACE_MAP[i]) for n, i in shutdown if n == node]
        if native:
            for _, interface in disabled:
                statement = r"^interface " + re.escape(interface) + r"\n"
                if len(re.findall(statement, config, re.M)) != 1:
                    raise ValueError("Unknown or ambiguous XR shutdown interface")
                config = re.sub(
                    r"(^interface " + re.escape(interface) + r"\n)(.*?)(?=\n!|\Z)",
                    lambda m: m[1] + " shutdown\n" + m[2].replace(" no shutdown", ""),
                    config,
                    flags=re.S | re.M,
                )
            content = "!! IOS XR Configuration 7.0.0\n" + config
        else:
            content = _cumulus_frr_snapshot(
                node,
                "frr version 10.5\nfrr defaults traditional\n"
                + (project_routeros_isp if isp else project_routeros_bgp)(
                    node, config, shutdown=disabled
                ),
            )
        (root / "configs" / f"{node}.cfg").write_text(content)
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
                (
                    n,
                    env.BGP_INTERFACE_MAP[i]
                    if n in configs and routing_model != "routeros-isp"
                    else i,
                )
                for n, i in endpoints
            ]
            edges.append(_edge(*endpoints[0], *endpoints[1]))
    (root / "batfish" / "layer1_topology.json").write_text(json.dumps({"edges": edges}))
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=root,
        metadata={
            "routing_model": routing_model,
            "native_config_analysis": native,
            **({"interface_aliases": aliases} if isp else {}),
            "scope": "Native IOS-XR IPv4 OSPF/BGP configuration with routed Linux hosts"
            if native and isp
            else "Restricted concrete RouterOS IPv4 OSPFv2 area 0, BGP/RR, exact-prefix filters and local preference; native execution required"
            if isp
            else "Native IOS-XR IPv4 BGP configuration with routed Linux hosts"
            if native
            else "Restricted concrete RouterOS IPv4 direct-eBGP, connected origins and exact-prefix export filters; native execution required",
        },
    )
