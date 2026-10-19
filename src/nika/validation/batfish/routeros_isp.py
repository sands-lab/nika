"""Strict projection of concrete RouterOS IPv4 ISP commands into FRR."""

from __future__ import annotations

import re
import shlex
from ipaddress import IPv4Address, IPv4Interface, IPv4Network


def routeros_model_interface(name: str) -> str:
    """Use Cumulus physical/loopback names for concrete RouterOS interfaces."""
    if name.startswith("ether"):
        return f"eth{int(name[5:]) - 1}"
    return "lo" if name == "lo0" else name


def _attributes(parts, required, optional=()):
    values = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if not separator or key in values:
            raise ValueError("Unsupported or duplicate RouterOS attribute")
        values[key] = value
    if not set(required) <= values.keys() or values.keys() - set(required) - set(
        optional
    ):
        raise ValueError(f"Unsupported RouterOS ISP attributes: {values}")
    return values


def _integer(value, maximum):
    if not value.isdecimal() or not 1 <= int(value) <= maximum:
        raise ValueError(f"Unsupported RouterOS integer: {value}")
    return value


def project_routeros_isp(node, config, *, shutdown=()):
    """Model only explicit interfaces, OSPFv2 and filtered IPv4 BGP commands.

    Every routing decision comes from the candidate text. Unknown commands,
    attributes, filter actions and interface bindings fail closed.
    """
    bridges, addresses, ospf_instances, areas, templates = (
        {},
        {},
        [],
        [],
        [],
    )
    bgp_instances, connections, filters, origins = [], [], {}, {}
    identity = False
    for line in config.splitlines():
        if not line.strip():
            continue
        parts = shlex.split(line)
        command = tuple(parts[:4])
        if parts[:3] == ["/system", "identity", "set"]:
            values = _attributes(parts[3:], ["name"])
            if values["name"] != node or identity:
                raise ValueError("Unsupported RouterOS identity")
            identity = True
        elif parts[:3] == ["/interface", "bridge", "add"]:
            values = _attributes(parts[3:], ["name", "protocol-mode"])
            if (
                values["name"] not in {"lo0", "lo1"}
                or values["protocol-mode"] != "none"
                or values["name"] in bridges
            ):
                raise ValueError("Unsupported RouterOS loopback bridge")
            bridges[values["name"]] = values
        elif parts[:3] == ["/ip", "address", "add"]:
            values = _attributes(parts[3:], ["address", "interface"])
            interface = values["interface"]
            if not re.fullmatch(r"ether[1-9][0-9]*|lo[01]", interface):
                raise ValueError("Unsupported RouterOS interface")
            address = IPv4Interface(values["address"])
            if any(
                address.ip == old.ip
                for entries in addresses.values()
                for old in entries
            ):
                raise ValueError("Duplicate RouterOS IPv4 address")
            addresses.setdefault(interface, []).append(address)
        elif command == ("/routing", "ospf", "instance", "add"):
            ospf_instances.append(
                _attributes(parts[4:], ["name", "version", "router-id"])
            )
        elif command == ("/routing", "ospf", "area", "add"):
            areas.append(_attributes(parts[4:], ["name", "instance", "area-id"]))
        elif command == ("/routing", "ospf", "interface-template", "add"):
            attributes = parts[4:]
            if attributes.count("passive") > 1:
                raise ValueError("Duplicate RouterOS OSPF passive flag")
            values = _attributes(
                [part for part in attributes if part != "passive"],
                ["area", "interfaces", "networks", "type", "cost"],
            )
            values["passive"] = "yes" if "passive" in attributes else "no"
            templates.append(values)
        elif command == ("/routing", "bgp", "instance", "add"):
            bgp_instances.append(
                _attributes(parts[4:], ["name", "as", "router-id"], ["cluster-id"])
            )
        elif command == ("/routing", "bgp", "connection", "add"):
            connections.append(
                _attributes(
                    parts[4:],
                    [
                        "name",
                        "instance",
                        "local.role",
                        "local.address",
                        "remote.address",
                        "remote.as",
                        "afi",
                        "connect",
                        "listen",
                        "input.filter",
                        "output.filter-chain",
                    ],
                    ["output.network", "nexthop-choice"],
                )
            )
        elif command == ("/routing", "filter", "rule", "add"):
            values = _attributes(parts[4:], ["chain", "rule"])
            filters.setdefault(values["chain"], []).append(values["rule"])
        elif command == ("/ip", "firewall", "address-list", "add"):
            values = _attributes(parts[4:], ["list", "address"])
            origins.setdefault(values["list"], []).append(
                str(IPv4Network(values["address"]))
            )
        else:
            raise ValueError(f"Unsupported RouterOS ISP statement: {line}")
    if not identity or len(ospf_instances) != 1 or len(areas) != 1:
        raise ValueError("RouterOS ISP projection requires one OSPF instance and area")
    if any(name.startswith("lo") and name not in bridges for name in addresses):
        raise ValueError("Undefined RouterOS loopback bridge")
    instance, area = ospf_instances[0], areas[0]
    if (
        instance["version"] != "2"
        or area["instance"] != instance["name"]
        or area["area-id"] != "0.0.0.0"
    ):
        raise ValueError("RouterOS ISP projection requires OSPFv2 area 0")
    IPv4Address(instance["router-id"])
    lines = [f"hostname {node}", "!"]
    attachments = {}
    for template in templates:
        name = template["interfaces"]
        if (
            name not in addresses
            or name in attachments
            or template["area"] != area["name"]
        ):
            raise ValueError("Unsupported RouterOS OSPF interface binding")
        network = IPv4Network(template["networks"])
        if not any(a.network == network for a in addresses[name]):
            raise ValueError("RouterOS OSPF template does not match its interface")
        if template["type"] not in {"broadcast", "ptp"} or template["passive"] not in {
            "yes",
            "no",
        }:
            raise ValueError(
                "Unsupported RouterOS OSPF interface type or passive state"
            )
        _integer(template["cost"], 65535)
        attachments[name] = template
    for name, entries in addresses.items():
        lines.append(f"interface {routeros_model_interface(name)}")
        lines.extend(f" ip address {address}" for address in entries)
        if (node, name) in shutdown:
            lines.append(" shutdown")
        if name in attachments:
            template = attachments[name]
            lines.append(f" ip ospf cost {template['cost']}")
            if template["type"] == "ptp":
                lines.append(" ip ospf network point-to-point")
        lines.append("!")
    lines.extend(["router ospf", f" router-id {instance['router-id']}"])
    for name, template in attachments.items():
        lines.extend(
            f" network {address.network} area 0.0.0.0" for address in addresses[name]
        )
        if template["passive"] == "yes":
            lines.append(f" passive-interface {routeros_model_interface(name)}")
    lines.append("!")
    if bgp_instances:
        if len(bgp_instances) != 1:
            raise ValueError("RouterOS ISP projection supports one BGP instance")
        lines.extend(
            _project_bgp(bgp_instances[0], connections, filters, origins, addresses)
        )
    elif connections or filters or origins:
        raise ValueError("RouterOS BGP commands require a concrete instance")
    return "\n".join(lines) + "\n"


def _project_bgp(instance, connections, filters, origins, addresses):
    policies, prefixes = {}, set()
    for chain, rules in filters.items():
        terms = []
        for rule in rules:
            match = re.fullmatch(r"if \(dst == ([0-9./]+)\) \{ (.+) \}", rule)
            prefix, action = match.groups() if match else (None, rule)
            if prefix:
                prefix = str(IPv4Network(prefix))
                prefixes.add(prefix)
            preference = None
            if action.startswith("set bgp-local-pref "):
                setting = re.fullmatch(r"set bgp-local-pref ([0-9]+); (.+)", action)
                if not setting:
                    raise ValueError("Unsupported RouterOS local-preference action")
                preference, action = setting.groups()
                _integer(preference, 4294967295)
            if action not in {"accept;", "reject;"} and not re.fullmatch(
                r"jump [A-Za-z0-9_-]+;", action
            ):
                raise ValueError("Unsupported RouterOS ISP filter rule")
            terms.append((prefix, action, preference))
        if not terms or terms[-1][0] is not None:
            raise ValueError("RouterOS filters require an explicit terminal action")
        policies[chain] = terms

    def evaluate(chain, prefix, seen=(), preference=None):
        if chain in seen or chain not in policies:
            raise ValueError("Undefined or recursive RouterOS filter chain")
        for match, action, local_preference in policies[chain]:
            if match is not None and match != prefix:
                continue
            preference = local_preference or preference
            if action in {"accept;", "reject;"}:
                return action == "accept;", preference
            return evaluate(action[5:-1], prefix, (*seen, chain), preference)
        raise ValueError("RouterOS filter chain has no terminal action")

    lines = []
    for index, chain in enumerate(policies):
        # The emitted grammar ends at a reject for all unspecified prefixes.
        if evaluate(chain, None)[0]:
            raise ValueError("RouterOS ISP filters must reject unspecified prefixes")
        for seq, prefix in enumerate(sorted(prefixes), 1):
            permit, preference = evaluate(chain, prefix)
            name = f"ROS-{index}-{seq}"
            lines.extend(
                [
                    f"ip prefix-list {name} seq 5 permit {prefix}",
                    f"route-map {chain} {'permit' if permit else 'deny'} {seq * 10}",
                    f" match ip address prefix-list {name}",
                ]
            )
            if permit and preference is not None:
                lines.append(f" set local-preference {preference}")
            lines.append("!")
    _integer(instance["as"], 4294967295)
    IPv4Address(instance["router-id"])
    lines.extend(
        [f"router bgp {instance['as']}", f" bgp router-id {instance['router-id']}"]
    )
    if "cluster-id" in instance:
        IPv4Address(instance["cluster-id"])
        lines.append(f" bgp cluster-id {instance['cluster-id']}")
    origin_prefixes = set()
    addressed = {
        str(a.ip): name for name, entries in addresses.items() for a in entries
    }
    peers = set()
    if len({connection.get("output.network") for connection in connections}) > 1:
        raise ValueError(
            "Per-neighbor RouterOS origin lists are outside the projection"
        )
    for connection in connections:
        if (
            connection["instance"] != instance["name"]
            or connection["local.address"] not in addressed
            or connection["remote.address"] in peers
        ):
            raise ValueError(
                "Unsupported RouterOS BGP instance or local-address binding"
            )
        peers.add(connection["remote.address"])
        IPv4Address(connection["remote.address"])
        _integer(connection["remote.as"], 4294967295)
        role = connection["local.role"]
        if (
            role not in {"ibgp", "ibgp-rr", "ebgp"}
            or connection["afi"] != "ip"
            or connection["connect"] != "yes"
            or connection["listen"] != "yes"
        ):
            raise ValueError("Unsupported RouterOS BGP role or transport")
        if (role == "ebgp") == (connection["remote.as"] == instance["as"]):
            raise ValueError("RouterOS BGP role and autonomous systems disagree")
        for key in ("input.filter", "output.filter-chain"):
            if connection[key] not in policies:
                raise ValueError("Undefined RouterOS BGP filter chain")
        source = routeros_model_interface(addressed[connection["local.address"]])
        lines.extend(
            [
                f" neighbor {connection['remote.address']} remote-as {connection['remote.as']}",
                f" neighbor {connection['remote.address']} update-source {source}",
            ]
        )
        if "output.network" in connection:
            # An absent address-list has no members, including after withdrawal
            # of its final concrete entry by an origin-loss control.
            origin_prefixes.update(origins.get(connection["output.network"], ()))
    if any(
        IPv4Network(p)
        not in {a.network for entries in addresses.values() for a in entries}
        for p in origin_prefixes
    ):
        raise ValueError("RouterOS BGP network lacks a concrete connected route")
    lines.append(" address-family ipv4 unicast")
    lines.extend(f"  network {prefix}" for prefix in sorted(origin_prefixes))
    for connection in connections:
        peer = connection["remote.address"]
        lines.extend(
            [
                f"  neighbor {peer} activate",
                f"  neighbor {peer} route-map {connection['input.filter']} in",
                f"  neighbor {peer} route-map {connection['output.filter-chain']} out",
            ]
        )
        if connection["local.role"] == "ibgp-rr":
            lines.append(f"  neighbor {peer} route-reflector-client")
        if "nexthop-choice" in connection:
            choice = connection["nexthop-choice"]
            if choice == "default":
                continue
            # FRR next-hop-self does not rewrite reflected iBGP routes, unlike
            # RouterOS force-self. Do not silently equate these operations.
            if choice != "force-self" or connection["local.role"] != "ebgp":
                raise ValueError("Unsupported RouterOS BGP next-hop mode")
            lines.append(f"  neighbor {peer} next-hop-self")
    lines.extend([" exit-address-family", "!"])
    return lines
