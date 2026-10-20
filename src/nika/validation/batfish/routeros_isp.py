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


def project_routeros_isp(node, config, *, shutdown=(), require_igp=True):
    """Model only explicit interfaces, OSPFv2 and filtered IPv4 BGP commands.

    Every routing decision comes from the candidate text. Unknown commands,
    attributes, filter actions and interface bindings fail closed.
    """
    if any(
        line.startswith(("/system/", "/interface/", "/ip/", "/routing/"))
        for line in config.splitlines()
    ):
        return project_routeros_isp(
            node,
            _normalize_netlab_routeros(node, config),
            shutdown=shutdown,
            require_igp=require_igp,
        )
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
    if not identity:
        raise ValueError("RouterOS projection requires a concrete identity")
    has_igp = bool(ospf_instances or areas or templates)
    if (require_igp or has_igp) and (len(ospf_instances) != 1 or len(areas) != 1):
        raise ValueError("RouterOS ISP projection requires one OSPF instance and area")
    if any(name.startswith("lo") and name not in bridges for name in addresses):
        raise ValueError("Undefined RouterOS loopback bridge")
    instance, area = (ospf_instances[0], areas[0]) if has_igp else (None, None)
    if has_igp:
        if instance["version"] != "2" or area["instance"] != instance["name"]:
            raise ValueError("RouterOS ISP projection requires one OSPFv2 area")
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
    if has_igp:
        lines.extend(["router ospf", f" router-id {instance['router-id']}"])
        for name, template in attachments.items():
            lines.extend(
                f" network {address.network} area {area['area-id']}"
                for address in addresses[name]
            )
            if template["passive"] == "yes":
                lines.append(f" passive-interface {routeros_model_interface(name)}")
        lines.append("!")
    if bgp_instances:
        if len(bgp_instances) != 1:
            raise ValueError("RouterOS ISP projection supports one BGP instance")
        lines.extend(
            _project_bgp(
                bgp_instances[0],
                connections,
                filters,
                origins,
                addresses,
                allow_default_input=not require_igp,
            )
        )
    elif connections or filters or origins:
        raise ValueError("RouterOS BGP commands require a concrete instance")
    return "\n".join(lines) + "\n"


def _normalize_netlab_routeros(node, config):
    """Project the upstream RouterOS 7 CLI set/instance/template subset."""
    addresses, bridges, ospf, area, templates = [], set(), {}, {}, []
    bgp_templates, bgp_instances, connections, filters, origins = {}, {}, {}, {}, {}
    housekeeping = {}
    lines = [f"/system identity set name={node}"]

    def command(line):
        refs = {}

        def preserve(match):
            token = f"__ROBRACKET_{len(refs)}__"
            refs[token] = match[0]
            return token

        parts = shlex.split(re.sub(r"\[[^\]]*\]", preserve, line))
        parts = [
            next(
                (
                    part.replace(token, value)
                    for token, value in refs.items()
                    if token in part
                ),
                part,
            )
            for part in parts
        ]
        if not parts:
            return (), []
        if parts[0].startswith("/"):
            if parts[0] == "/routing":
                return ("routing", *parts[1:4]), parts[4:]
            path = tuple(part for part in parts[0].split("/") if part)
            if path and path[-1] in {
                "add",
                "set",
                "remove",
                "enable",
                "disable",
                "print",
            }:
                return path, parts[1:]
            verb = parts[1:2]
            return (*path, *verb), parts[2:]
        return tuple(parts[:4]), parts[4:]

    def attrs(raw):
        values = {}
        for item in raw:
            if item.startswith("["):
                continue
            if "=" not in item:
                if item == "passive" or re.fullmatch(r"ether[1-9][0-9]*|lo1", item):
                    continue
                raise ValueError(f"Unsupported RouterOS netlab token: {item!r}")
            key, value = item.split("=", 1)
            if key in values:
                raise ValueError("Duplicate RouterOS netlab attribute")
            values[key] = value
        return values

    def key_from_selector(line, menu, kind="name"):
        match = re.search(rf"\[{re.escape(menu)} find {kind}=([^\]]+)\]", line)
        if not match:
            raise ValueError("Unsupported RouterOS netlab set selector")
        return match[1]

    for source in config.splitlines():
        line = source.strip()
        if not line or line.startswith(("#", "!")):
            continue
        path, raw = command(line)
        values = attrs(raw)
        if path == ("system", "identity", "set"):
            if values != {"name": node}:
                raise ValueError("Unsupported RouterOS netlab identity")
        elif path == ("interface", "bridge", "add"):
            name = values.get("name")
            if name not in {"loopback", "lo1"} or values.get("protocol-mode") != "none":
                raise ValueError("Unsupported RouterOS netlab bridge")
            bridges.add("lo0" if name == "loopback" else name)
        elif path == ("ip", "dns", "static", "add"):
            if set(values) != {"name", "address"}:
                raise ValueError("Unsupported RouterOS netlab DNS entry")
        elif path == ("ip", "address", "add"):
            if set(values) != {"interface", "address"}:
                raise ValueError("Unsupported RouterOS netlab address")
            interface = {"loopback": "lo0"}.get(
                values["interface"], values["interface"]
            )
            addresses.append({"interface": interface, "address": values["address"]})
        elif path == ("interface", "ethernet", "set"):
            if (
                set(values) != {"comment"}
                or not raw
                or not re.fullmatch(r"ether[1-9][0-9]*|lo1", raw[-1])
            ):
                raise ValueError("Unsupported RouterOS netlab interface metadata")
        elif path == ("ip", "address", "set"):
            if set(values) != {"comment"} or "[find where interface=" not in line:
                raise ValueError("Unsupported RouterOS netlab address metadata")
        elif path == ("ip", "neighbor", "discovery-settings", "set"):
            if values != {"discover-interface-list": "all"}:
                raise ValueError("Unsupported RouterOS netlab discovery setting")
        elif path == ("routing", "ospf", "instance", "add"):
            if (
                set(values) != {"name", "version", "router-id", "vrf"}
                or values["vrf"] != "main"
            ):
                raise ValueError("Unsupported RouterOS netlab OSPF instance")
            ospf.update(values)
        elif path == ("routing", "ospf", "area", "add"):
            if set(values) != {"instance", "name", "area-id"}:
                raise ValueError("Unsupported RouterOS netlab OSPF area")
            if area:
                raise ValueError("RouterOS netlab projection supports one OSPF area")
            area.update(values)
        elif path == ("routing", "ospf", "interface-template", "add"):
            if not values.get("area", "").startswith("[/routing"):
                raise ValueError("Unsupported RouterOS netlab OSPF area selector")
            if "passive" in raw:
                values["passive"] = "yes"
            templates.append(values)
        elif path == ("routing", "bgp", "template", "add"):
            if set(values) != {"name", "as", "output.network"}:
                raise ValueError("Unsupported RouterOS netlab BGP template")
            bgp_templates[values["name"]] = values
        elif path == ("routing", "bgp", "instance", "add"):
            if set(values) != {"name", "as"}:
                raise ValueError("Unsupported RouterOS netlab BGP instance")
            bgp_instances[values["name"]] = values
        elif path == ("routing", "bgp", "instance", "set"):
            name = key_from_selector(line, "/routing/bgp/instance")
            if name not in bgp_instances or set(values) - {"router-id", "cluster-id"}:
                raise ValueError("Unsupported RouterOS netlab BGP instance update")
            bgp_instances[name].update(values)
        elif path == ("routing", "bgp", "connection", "add"):
            if set(values) != {
                "template",
                "instance",
                "name",
                "remote.address",
                "remote.as",
                "comment",
                "local.role",
            }:
                raise ValueError("Unsupported RouterOS netlab BGP connection")
            connections[values["name"]] = dict(values)
        elif path == ("routing", "bgp", "connection", "set"):
            name = key_from_selector(line, "/routing/bgp/connection")
            if name not in connections or set(values) - {
                "afi",
                "output.filter-chain",
                "local.role",
                "local.address",
                "input.filter",
            }:
                raise ValueError("Unsupported RouterOS netlab BGP connection update")
            connections[name].update(values)
        elif path == ("routing", "filter", "rule", "add"):
            if set(values) - {"chain", "rule", "place-before"} or not {
                "chain",
                "rule",
            } <= set(values):
                raise ValueError("Unsupported RouterOS netlab filter")
            chain = values["chain"]
            # netlab emits community scrubbing; it does not affect the modeled
            # prefix acceptance behavior. Only accept known housekeeping forms.
            rule = values["rule"]
            if "place-before" in values and (
                values["place-before"] != "0"
                or not re.fullmatch(r"jump rm_[a-z_]+;", rule)
            ):
                raise ValueError(
                    "Only upstream community housekeeping may prepend a rule"
                )
            if chain.startswith("rm_") or chain == "filter_rr_self_ebgp":
                housekeeping.setdefault(chain, []).append(rule)
                continue
            if chain.startswith("filter_"):
                if rule not in {"accept;", "return;"} and not re.fullmatch(
                    r"jump rm_[a-z_]+;", rule
                ):
                    raise ValueError("Unsupported RouterOS netlab peer filter")
                continue
            filters.setdefault(chain, []).append(rule)
        elif path == ("ip", "firewall", "address-list", "add"):
            if set(values) != {"list", "address"}:
                raise ValueError("Unsupported RouterOS netlab origin list")
            origins.setdefault(values["list"], []).append(values["address"])
        else:
            raise ValueError(f"Unsupported RouterOS netlab statement: {source}")

    known_housekeeping = {
        "rm_extended_communities": [
            "delete bgp-ext-communities rt; delete bgp-ext-communities soo; delete bgp-ext-communities other;",
            "return;",
        ],
        "rm_large_communities": [
            "delete bgp-large-communities all;",
            "return;",
        ],
        "rm_standard_communities": [
            "delete bgp-communities other; delete bgp-communities wk;",
            "jump rm_large_communities;",
            "return;",
        ],
    }
    for chain, rules in housekeeping.items():
        if chain == "filter_rr_self_ebgp":
            if rules[-1:] != ["return;"] or any(
                not re.fullmatch(
                    r"if \( bgp-input-remote-addr == [0-9.]+ \) \{ set gw [0-9.]+;\}",
                    rule,
                )
                for rule in rules[:-1]
            ):
                raise ValueError("Unsupported RouterOS netlab next-hop helper")
        elif known_housekeeping.get(chain) != rules:
            raise ValueError("Unsupported RouterOS netlab community filter")

    for chain, rules in filters.items():
        filters[chain] = [
            rule
            for rule in rules
            if not (
                (match := re.fullmatch(r"jump (rm_[a-z_]+);", rule))
                and match[1] in known_housekeeping
            )
        ]
    if any("input.filter" not in connection for connection in connections.values()):
        if "NETLAB-ACCEPT" in filters:
            raise ValueError(
                "Reserved implicit input policy already exists in candidate"
            )
        filters["NETLAB-ACCEPT"] = ["accept;"]
    lines.extend(
        f"/interface bridge add name={name} protocol-mode=none"
        for name in sorted(bridges)
    )
    lines.extend(
        f"/ip address add address={a['address']} interface={a['interface']}"
        for a in addresses
    )
    if ospf:
        if not area or len(templates) == 0:
            raise ValueError(
                "RouterOS netlab OSPF requires an area and interface templates"
            )
        lines.append(
            f"/routing ospf instance add name={ospf['name']} version={ospf['version']} router-id={ospf['router-id']}"
        )
        lines.append(
            f"/routing ospf area add name={area['name']} instance={area['instance']} area-id={area['area-id']}"
        )
        address_map = {}
        for a in addresses:
            address_map.setdefault(a["interface"], []).append(
                IPv4Interface(a["address"])
            )
        for template in templates:
            if (
                template["area"].find(f"area-id={area['area-id']}") < 0
                or template["area"].find(f"instance={ospf['name']}") < 0
            ):
                raise ValueError("Unsupported RouterOS netlab OSPF area binding")
            interface = {"loopback": "lo0"}.get(
                template.get("interface"), template.get("interface")
            )
            if interface not in address_map:
                raise ValueError("RouterOS netlab OSPF interface lacks an address")
            for address in address_map[interface]:
                cost = template.get("cost", "1")
                kind = template.get("type", "broadcast")
                passive = (
                    " passive" if interface == "lo0" or "passive" in template else ""
                )
                lines.append(
                    f"/routing ospf interface-template add area={area['name']} interfaces={interface} networks={address.network} type={kind} cost={cost}{passive}"
                )
    if bgp_instances:
        if len(bgp_instances) != 1:
            raise ValueError("RouterOS netlab supports one BGP instance")
        instance = next(iter(bgp_instances.values()))
        template = (
            bgp_templates.get(next(iter(connections.values()))["template"])
            if connections
            else None
        )
        if not template:
            raise ValueError("RouterOS netlab BGP requires an inherited template")
        lines.append(
            f"/routing bgp instance add name={instance['name']} as={instance['as']} router-id={instance.get('router-id', ospf.get('router-id'))}"
            + (
                f" cluster-id={instance['cluster-id']}"
                if instance.get("cluster-id")
                else ""
            )
        )
        for chain, rules in filters.items():
            for rule in rules:
                lines.append(f'/routing filter rule add chain={chain} rule="{rule}"')
        for name, connection in connections.items():
            parent = bgp_templates.get(connection["template"])
            if not parent or connection["instance"] != instance["name"]:
                raise ValueError("Unsupported RouterOS BGP template inheritance")
            local = connection.get("local.address")
            if local is None and connection["local.role"] == "ebgp":
                (local,) = [
                    str(IPv4Interface(row["address"]).ip)
                    for row in addresses
                    if IPv4Address(connection["remote.address"])
                    in IPv4Interface(row["address"]).network
                ]
            if local in (None, "loopback"):
                local = str(
                    next(a for a in addresses if a["interface"] == "lo0")["address"]
                ).split("/")[0]
            lines.append(
                "/routing bgp connection add "
                f"name={name} instance={connection['instance']} local.role={connection['local.role']} "
                f"local.address={local} remote.address={connection['remote.address']} remote.as={connection['remote.as']} "
                f"afi={connection.get('afi', 'ip')} connect=yes listen=yes "
                f"input.filter={connection.get('input.filter', 'NETLAB-ACCEPT')} "
                f"output.filter-chain={connection.get('output.filter-chain', 'NIKA-BGP-OUT')} "
                f"output.network={parent['output.network']}"
            )
        for list_name, entries in origins.items():
            for address in entries:
                lines.append(
                    f"/ip firewall address-list add list={list_name} address={address}"
                )
    return "\n".join(lines) + "\n"


def _project_bgp(
    instance, connections, filters, origins, addresses, *, allow_default_input=False
):
    policies, prefixes = {}, set()
    for chain, rules in filters.items():
        terms = []
        for rule in rules:
            match = re.fullmatch(
                r"if \(dst == ([0-9./]+)(?: && dst-len == ([0-9]+))?\) \{ (.+) \}", rule
            )
            prefix, length, action = match.groups() if match else (None, None, rule)
            if length is not None and int(length) != IPv4Network(prefix).prefixlen:
                raise ValueError("RouterOS prefix and exact mask length disagree")
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
            raise ValueError(
                f"Undefined or recursive RouterOS filter chain {chain!r}; known={sorted(policies)}"
            )
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
        if (
            allow_default_input
            and chain == "NETLAB-ACCEPT"
            and policies[chain] == [(None, "accept;", None)]
            and all(c["output.filter-chain"] != chain for c in connections)
        ):
            lines.extend([f"route-map {chain} permit 10", "!"])
            continue
        # ISP policies must reject unspecified prefixes; direct eBGP may use
        # the platform's no-input-policy accept-all behavior above.
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
