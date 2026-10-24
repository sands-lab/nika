"""Strict projections of concrete RouterOS IPv4 configurations into FRR."""

from __future__ import annotations

import re
import shlex
from types import SimpleNamespace
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
    parsed = _parse_isp(node, config)
    if parsed.isis_instances or parsed.isis_templates:
        raise ValueError("RouterOS IS-IS is analyzed through project_routeros_isis")
    bridges, addresses, templates = parsed.bridges, parsed.addresses, parsed.templates
    ospf_instances, areas = parsed.ospf_instances, parsed.areas
    bgp_instances, connections = parsed.bgp_instances, parsed.connections
    filters, origins = parsed.filters, parsed.origins
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


def _parse_isp(node, config):
    """Parse the ISP renderer's statements; fail closed on anything else."""
    bridges, addresses, ospf_instances, areas, templates = (
        {},
        {},
        [],
        [],
        [],
    )
    bgp_instances, connections, filters, origins = [], [], {}, {}
    isis_instances, isis_templates = [], []
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
        elif command == ("/routing", "isis", "instance", "add"):
            isis_instances.append(
                _attributes(
                    parts[4:], ["name", "afi", "areas", "system-id", "metric-type"]
                )
            )
        elif command == ("/routing", "isis", "interface-template", "add"):
            flags = [part for part in parts[4:] if part in {"passive", "ptp"}]
            if len(set(flags)) != len(flags):
                raise ValueError("Duplicate RouterOS IS-IS interface flag")
            values = _attributes(
                [part for part in parts[4:] if part not in {"passive", "ptp"}],
                ["instance", "interfaces", "levels"],
                ["ptp.l2.metric"],
            )
            values.update(dict.fromkeys(flags, True))
            isis_templates.append(values)
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
    return SimpleNamespace(
        bridges=bridges,
        addresses=addresses,
        ospf_instances=ospf_instances,
        areas=areas,
        templates=templates,
        isis_instances=isis_instances,
        isis_templates=isis_templates,
        bgp_instances=bgp_instances,
        connections=connections,
        filters=filters,
        origins=origins,
    )


def project_routeros_isis(node, config, *, shutdown=()):
    """Project the RouterOS ISP IS-IS subset to IOS XR through its FRR form.

    RouterOS IS-IS statements become the FRR Level-2, wide-metric subset that
    ``project_frr_isis`` already translates for Batfish's IOS XR parser:
    point-to-point circuits keep their Level-2 metric, and passive circuits
    advertise their prefixes with metric 0, as RouterOS does. BGP keeps the
    concrete sessions, origins and filters of ``project_routeros_isp``.
    ``shutdown`` holds ``(node, RouterOS port)`` pairs.
    """
    from nika.net_env.isp.igp.frr import ISIS_PROCESS
    from nika.validation.batfish.isis import project_frr_isis

    parsed = _parse_isp(node, config)
    if parsed.ospf_instances or parsed.areas or parsed.templates:
        raise ValueError("RouterOS IS-IS projection excludes OSPF")
    if len(parsed.isis_instances) != 1:
        raise ValueError("RouterOS IS-IS projection requires one instance")
    instance = parsed.isis_instances[0]
    if instance["afi"] != "ip" or instance["metric-type"] != "wide":
        raise ValueError("RouterOS IS-IS projection requires IPv4 wide metrics")
    if not re.fullmatch(r"[0-9a-fA-F]{2}(?:\.[0-9a-fA-F]{4}){0,6}", instance["areas"]):
        raise ValueError("Unsupported RouterOS IS-IS area")
    if not re.fullmatch(
        r"(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}", instance["system-id"]
    ):
        raise ValueError("Unsupported RouterOS IS-IS system ID")
    addresses = parsed.addresses
    if any(name.startswith("lo") and name not in parsed.bridges for name in addresses):
        raise ValueError("Undefined RouterOS loopback bridge")
    circuits = {}
    for template in parsed.isis_templates:
        name = template["interfaces"]
        if (
            template["instance"] != instance["name"]
            or template["levels"] != "l2"
            or name not in addresses
            or name in circuits
            or name == "lo1"
            or len(addresses[name]) != 1
        ):
            raise ValueError("Unsupported RouterOS IS-IS interface binding")
        if template.get("passive"):
            if template.get("ptp") or "ptp.l2.metric" in template:
                raise ValueError("RouterOS passive IS-IS circuits take no metric")
            circuits[name] = None
        else:
            if not template.get("ptp") or name == "lo0":
                raise ValueError("RouterOS IS-IS projection requires p2p circuits")
            circuits[name] = int(_integer(template["ptp.l2.metric"], 16_777_214))
    lines = [
        "frr version 10.5.3",
        "frr defaults traditional",
        f"hostname {node}",
        "!",
    ]
    for name, entries in addresses.items():
        if name == "lo1":
            continue
        if len(entries) != 1:
            raise ValueError("RouterOS IS-IS projection requires one address per port")
        lines.extend([f"interface {routeros_model_interface(name)}"])
        lines.append(f" ip address {entries[0]}")
        if name in circuits:
            lines.append(f" ip router isis {ISIS_PROCESS}")
            if circuits[name] is None:
                if name != "lo0":
                    lines.append(" isis circuit-type level-2-only")
                lines.extend([" isis passive", " isis metric 0"])
            else:
                lines.extend(
                    [
                        " isis circuit-type level-2-only",
                        " isis network point-to-point",
                        f" isis metric {circuits[name]}",
                    ]
                )
        lines.append("!")
    lines.extend(
        [
            f"router isis {ISIS_PROCESS}",
            f" net {instance['areas']}.{instance['system-id']}.00",
            " is-type level-2-only",
            " metric-style wide",
            "!",
        ]
    )
    if "lo1" in addresses:
        if "lo1" not in parsed.bridges:
            raise ValueError("Undefined RouterOS loopback bridge")
        lines.append("interface lo1")
        lines.extend(f" ip address {address}" for address in addresses["lo1"])
        lines.append("!")
    if parsed.bgp_instances:
        if len(parsed.bgp_instances) != 1:
            raise ValueError("RouterOS ISP projection supports one BGP instance")
        lines.extend(
            _project_bgp(
                parsed.bgp_instances[0],
                parsed.connections,
                parsed.filters,
                parsed.origins,
                addresses,
                explicit_activation=True,
            )
        )
    elif parsed.connections or parsed.filters or parsed.origins:
        raise ValueError("RouterOS BGP commands require a concrete instance")
    return project_frr_isis(
        node,
        "\n".join(lines) + "\n",
        shutdown={(n, routeros_model_interface(i)) for n, i in shutdown},
        allow_ebgp=True,
    )


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
        elif path in {
            ("interface", "ethernet", "set"),
            ("interface", "bridge", "set"),
        }:
            interface_pattern = r"ether[1-9][0-9]*" if path[1] == "ethernet" else "lo1"
            if (
                set(values) != {"comment"}
                or not raw
                or not re.fullmatch(interface_pattern, raw[-1])
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
                f"output.filter-chain={connection.get('output.filter-chain', 'POLICY-BGP-OUT')} "
                f"output.network={parent['output.network']}"
            )
        for list_name, entries in origins.items():
            for address in entries:
                lines.append(
                    f"/ip firewall address-list add list={list_name} address={address}"
                )
    return "\n".join(lines) + "\n"


def _project_bgp(
    instance,
    connections,
    filters,
    origins,
    addresses,
    *,
    allow_default_input=False,
    explicit_activation=False,
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
    if explicit_activation:
        # Each neighbor below is activated explicitly, so FRR's implicit IPv4
        # activation is irrelevant; the IOS XR projection requires this form.
        lines.append(" no bgp default ipv4-unicast")
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


def routeros_router_interface(name: str) -> str:
    """Container name of a converted router's RouterOS port (``etherN+2`` is ``ethN``)."""
    if match := re.fullmatch(r"ether([2-9]|[1-9][0-9]+)", name):
        return f"eth{int(match[1]) - 2}"
    if name == "br0":
        return name
    raise ValueError(f"Unsupported RouterOS interface {name!r}")


_EXACT_RULE = re.compile(
    r"if \(dst == ([0-9./]+) && dst-len == ([0-9]+)\) \{ (accept|reject); \}"
)


def project_routeros_router(node, config, *, shutdown=()):
    """Project a router converted from an FRR sketch back to FRR for Batfish.

    Accept only the statements NIKA renders for converted routers: addresses on
    data ports and ``br0``, static routes, OSPFv2 network templates with costs
    and passive flags, and IPv4 BGP with connected origins and exact-prefix
    filter chains. Interfaces take their container names; ``shutdown`` lists
    container interfaces disabled in the analyzed state. The ``br0`` bridge,
    its ports and its DHCP relay are layer-2 and host services; as for FRR
    routers, only the bridge's routed address enters the model. Every other
    statement is rejected.
    """
    identity = None
    bridges, bridge_ports, addresses, routes = set(), [], {}, []
    ospf, areas, templates = [], {}, []
    bgp, connections, chains, origins = [], [], {}, {}
    for line in config.splitlines():
        if not line.strip():
            continue
        parts = shlex.split(line)
        # The menu path and verb precede the item's attributes and flags.
        split = next(
            (
                i
                for i, part in enumerate(parts)
                if "=" in part or part in {"passive", "blackhole"}
            ),
            len(parts),
        )
        path, raw = tuple(parts[:split]), parts[split:]
        if path == ("/system", "identity", "set"):
            values = _attributes(raw, ["name"])
            if values["name"] != node or identity:
                raise ValueError("Unsupported RouterOS identity")
            identity = node
        elif path == ("/interface", "bridge", "add"):
            values = _attributes(raw, ["name", "protocol-mode"])
            if values != {"name": "br0", "protocol-mode": "none"} or bridges:
                raise ValueError("Unsupported RouterOS bridge")
            bridges.add("br0")
        elif path == ("/interface", "bridge", "port", "add"):
            values = _attributes(raw, ["bridge", "interface"])
            if values["bridge"] not in bridges:
                raise ValueError("RouterOS bridge port precedes its bridge")
            bridge_ports.append(routeros_router_interface(values["interface"]))
        elif path == ("/ip", "address", "add"):
            values = _attributes(raw, ["address", "interface"])
            interface = routeros_router_interface(values["interface"])
            if interface == "br0" and "br0" not in bridges:
                raise ValueError("RouterOS address on an undefined bridge")
            address = IPv4Interface(values["address"])
            if interface in addresses or interface in bridge_ports:
                raise ValueError("Unsupported RouterOS interface addressing")
            addresses[interface] = address
        elif path == ("/ip", "route", "add"):
            if raw.count("blackhole") == 1:
                # A blackhole route discards matching traffic, like FRR Null0.
                values = _attributes(
                    [part for part in raw if part != "blackhole"], ["dst-address"]
                )
                gateway = "Null0"
            else:
                values = _attributes(raw, ["dst-address", "gateway"])
                gateway = str(IPv4Address(values["gateway"]))
            routes.append((str(IPv4Network(values["dst-address"])), gateway))
        elif path == ("/ip", "dhcp-relay", "add"):
            values = _attributes(
                raw, ["name", "interface", "dhcp-server", "local-address", "disabled"]
            )
            if values["interface"] != "br0" or values["disabled"] != "no":
                raise ValueError("Unsupported RouterOS DHCP relay")
            IPv4Address(values["dhcp-server"])
            IPv4Address(values["local-address"])
        elif path == ("/routing", "ospf", "instance", "add"):
            ospf.append(_attributes(raw, ["name", "version", "router-id"]))
        elif path == ("/routing", "ospf", "area", "add"):
            values = _attributes(raw, ["name", "instance", "area-id"])
            if values["name"] in areas:
                raise ValueError("Duplicate RouterOS OSPF area")
            areas[values["name"]] = values
        elif path == ("/routing", "ospf", "interface-template", "add"):
            flags = [part for part in raw if part == "passive"]
            values = _attributes(
                [part for part in raw if part != "passive"],
                ["area", "networks"],
                ["cost"],
            )
            if len(flags) > 1:
                raise ValueError("Duplicate RouterOS OSPF passive flag")
            values["passive"] = bool(flags)
            templates.append(values)
        elif path == ("/ip", "firewall", "address-list", "add"):
            values = _attributes(raw, ["list", "address"])
            origins.setdefault(values["list"], []).append(
                str(IPv4Network(values["address"]))
            )
        elif path == ("/routing", "filter", "rule", "add"):
            values = _attributes(raw, ["chain", "rule"])
            chains.setdefault(values["chain"], []).append(values["rule"])
        elif path == ("/routing", "bgp", "instance", "add"):
            bgp.append(_attributes(raw, ["name", "as", "router-id"]))
        elif path == ("/routing", "bgp", "connection", "add"):
            connections.append(
                _attributes(
                    raw,
                    [
                        "name",
                        "instance",
                        "local.role",
                        "remote.address",
                        "remote.as",
                        "afi",
                        "connect",
                        "listen",
                    ],
                    ["output.network", "input.filter", "output.filter-chain"],
                )
            )
        else:
            raise ValueError(f"Unsupported RouterOS router statement: {line}")
    if identity is None:
        raise ValueError("RouterOS projection requires a concrete identity")
    if not addresses:
        raise ValueError("RouterOS projection requires addressed interfaces")
    lines = [f"hostname {node}", "!"]
    costs, passive, networks = {}, set(), []
    if ospf or areas or templates:
        if len(ospf) != 1 or ospf[0]["version"] != "2" or not areas:
            raise ValueError("RouterOS projection requires one OSPFv2 instance")
        instance = ospf[0]
        IPv4Address(instance["router-id"])
        for area in areas.values():
            if area["instance"] != instance["name"]:
                raise ValueError("RouterOS OSPF area outside the instance")
            IPv4Address(area["area-id"])
        for template in templates:
            if template["area"] not in areas:
                raise ValueError("RouterOS OSPF template names an undefined area")
            network = IPv4Network(template["networks"])
            if network in [n for n, _ in networks]:
                raise ValueError("Duplicate RouterOS OSPF template network")
            networks.append((network, areas[template["area"]]["area-id"]))
            # RouterOS matches an interface by address; FRR's ``network`` needs
            # the interface prefix inside it. Project only where both agree.
            members = [
                name for name, address in addresses.items() if address.ip in network
            ]
            if any(not addresses[n].network.subnet_of(network) for n in members):
                raise ValueError("RouterOS OSPF template is narrower than its port")
            for name in members:
                # A port in two templates takes the first match on RouterOS and
                # the most specific network in FRR; reject the ambiguity.
                if name in costs:
                    raise ValueError("Overlapping RouterOS OSPF templates")
                # RouterOS interface templates default to cost 1.
                costs[name] = int(_integer(template.get("cost", "1"), 65535))
                if template["passive"]:
                    passive.add(name)
    for name, address in addresses.items():
        lines.append(f"interface {name}")
        lines.append(f" ip address {address}")
        if name in shutdown:
            lines.append(" shutdown")
        if name in costs:
            lines.append(f" ip ospf cost {costs[name]}")
        lines.append("!")
    lines.extend(f"ip route {prefix} {gateway}" for prefix, gateway in routes)
    if networks:
        lines.extend(["router ospf", f" ospf router-id {ospf[0]['router-id']}"])
        lines.extend(f" network {network} area {area}" for network, area in networks)
        lines.extend(f" passive-interface {name}" for name in sorted(passive))
        lines.append("!")
    lines.extend(_project_router_bgp(bgp, connections, chains, origins, addresses))
    return "\n".join(lines) + "\n"


def _project_router_bgp(instances, connections, chains, origins, addresses):
    if not instances:
        if connections or chains or origins:
            raise ValueError("RouterOS BGP commands require a concrete instance")
        return []
    if len(instances) != 1:
        raise ValueError("RouterOS projection supports one BGP instance")
    instance = instances[0]
    _integer(instance["as"], 4294967295)
    IPv4Address(instance["router-id"])
    policies, lists = [], []
    for chain, rules in chains.items():
        if rules[-1:] != ["reject;"]:
            raise ValueError("RouterOS filter chains must end with reject")
        for seq, rule in enumerate(rules[:-1], 1):
            match = _EXACT_RULE.fullmatch(rule)
            if not match:
                raise ValueError(f"Unsupported RouterOS filter rule: {rule}")
            prefix, length, action = match.groups()
            network = IPv4Network(prefix)
            if int(length) != network.prefixlen or str(network) != prefix:
                raise ValueError("RouterOS exact prefix and length disagree")
            name = f"ROS-{chain}-{seq}"
            lists.append(f"ip prefix-list {name} seq 5 permit {prefix}")
            policies.extend(
                [
                    f"route-map {chain} {'permit' if action == 'accept' else 'deny'} {seq * 10}",
                    f" match ip address prefix-list {name}",
                    "!",
                ]
            )
        if len(rules) == 1:
            # A chain that only rejects matches nothing; an empty FRR route
            # map denies every route as well.
            policies.extend([f"route-map {chain} deny 10", "!"])
    lists_used = {
        connection.get(key)
        for connection in connections
        for key in ("input.filter", "output.filter-chain")
    } - {None}
    if not lists_used <= set(chains):
        raise ValueError("Undefined RouterOS BGP filter chain")
    outputs = {connection.get("output.network") for connection in connections}
    if len(outputs) > 1:
        raise ValueError(
            "Per-neighbor RouterOS origin lists are outside the projection"
        )
    origin = outputs.pop() if outputs else None
    networks = sorted(origins.get(origin, ())) if origin else []
    if set(origins) - {origin}:
        raise ValueError("Unused RouterOS address lists are outside the projection")
    connected = {address.network for address in addresses.values()}
    if any(IPv4Network(network) not in connected for network in networks):
        raise ValueError("RouterOS BGP network lacks a concrete connected route")
    lines = [*lists, *policies]
    lines.extend(
        [
            f"router bgp {instance['as']}",
            f" bgp router-id {instance['router-id']}",
            # A RouterOS session without a filter accepts and advertises every route.
            " no bgp ebgp-requires-policy",
        ]
    )
    peers = set()
    for connection in connections:
        peer = str(IPv4Address(connection["remote.address"]))
        _integer(connection["remote.as"], 4294967295)
        if (
            connection["instance"] != instance["name"]
            or peer in peers
            or connection["afi"] != "ip"
            or connection["connect"] != "yes"
            or connection["listen"] != "yes"
            or connection["local.role"] not in {"ebgp", "ibgp"}
            or (connection["local.role"] == "ebgp")
            == (connection["remote.as"] == instance["as"])
        ):
            raise ValueError("Unsupported RouterOS BGP connection")
        peers.add(peer)
        lines.append(f" neighbor {peer} remote-as {connection['remote.as']}")
    lines.append(" address-family ipv4 unicast")
    lines.extend(f"  network {network}" for network in networks)
    for connection in connections:
        peer = connection["remote.address"]
        for key, direction in (("input.filter", "in"), ("output.filter-chain", "out")):
            if key in connection:
                lines.append(
                    f"  neighbor {peer} route-map {connection[key]} {direction}"
                )
    lines.extend([" exit-address-family", "!"])
    return lines


# Statements outside the Linux default-table model. As for FRR edges, their
# overlay, VRF, NAT, filtering and QoS behavior is declared as runtime-only.
_RUNTIME_ONLY = (
    ("/ip", "vrf", "add"),
    ("/interface", "wireguard", "add"),
    ("/interface", "wireguard", "peers", "add"),
    ("/ip", "firewall", "nat", "add"),
    ("/ip", "firewall", "filter", "add"),
    ("/ip", "firewall", "mangle", "add"),
    ("/queue", "type", "add"),
    ("/queue", "tree", "add"),
    ("/routing", "filter", "rule", "add"),
    ("/routing", "bgp", "instance", "add"),
    ("/routing", "bgp", "connection", "add"),
    ("/routing", "bgp", "vpn", "add"),
)


def project_routeros_static_underlay(node, config, *, shutdown=()):
    """Project a RouterOS router's default (``main``) table addressing and routes.

    This is the RouterOS counterpart of the Linux static projection of FRR
    edges: addresses on data ports outside any VRF and ``main`` static routes
    with IPv4 gateways. VRF, tunnel, BGP, NAT, filter and QoS statements are
    recognized and left to runtime requirements when they stay in a declared
    VRF; any other statement, or a route or BGP instance in another table, is
    rejected.
    """
    statements = []
    for line in config.splitlines():
        if not line.strip() or line.strip() == ":delay 2s":
            continue
        parts = shlex.split(line)
        split = next((i for i, part in enumerate(parts) if "=" in part), len(parts))
        values = dict(part.split("=", 1) for part in parts[split:] if "=" in part)
        if len(values) != len(parts) - split:
            raise ValueError(f"Unsupported RouterOS flag: {line}")
        statements.append((line, tuple(parts[:split]), values))
    # A VRF claims its ports wherever the statement appears in the script.
    vrfs, vrf_ports = set(), set()
    for _, path, values in statements:
        if path == ("/ip", "vrf", "add"):
            if not values.get("name") or not values.get("interfaces"):
                raise ValueError("Unsupported RouterOS VRF")
            vrfs.add(values["name"])
            vrf_ports.update(values["interfaces"].split(","))
    identity, addresses, routes = None, {}, []
    for line, path, values in statements:
        if path == ("/system", "identity", "set"):
            if values != {"name": node} or identity:
                raise ValueError("Unsupported RouterOS identity")
            identity = node
        elif path == ("/routing", "bgp", "instance", "add"):
            # BGP routes in ``main`` would change the analyzed table.
            if values.get("vrf") not in vrfs:
                raise ValueError("RouterOS BGP outside a VRF changes the main table")
        elif path in _RUNTIME_ONLY:
            continue
        elif path == ("/ip", "address", "add"):
            if set(values) != {"address", "interface"}:
                raise ValueError("Unsupported RouterOS addressing")
            if values["interface"] in vrf_ports:
                continue
            interface = routeros_router_interface(values["interface"])
            addresses.setdefault(interface, []).append(IPv4Interface(values["address"]))
        elif path == ("/ip", "route", "add"):
            gateway = values.get("gateway", "")
            table = values.get("routing-table", "main")
            if table != "main":
                if table not in vrfs:
                    raise ValueError("RouterOS route into an undeclared table")
                continue
            if "@" in gateway:
                # A cross-VRF route into a VRF port, like FRR's ``dev vrf_*``
                # leak, is outside the default-table model.
                port, _, vrf = gateway.partition("@")
                if (
                    port not in vrf_ports
                    or vrf not in vrfs
                    or set(values) != {"dst-address", "gateway"}
                ):
                    raise ValueError("Unsupported RouterOS cross-table route")
                continue
            if set(values) != {"dst-address", "gateway"}:
                raise ValueError("Unsupported RouterOS static route")
            routes.append(
                (
                    str(IPv4Network(values["dst-address"])),
                    str(IPv4Address(values["gateway"])),
                )
            )
        else:
            raise ValueError(f"Unsupported RouterOS statement: {line}")
    if identity is None:
        raise ValueError("RouterOS projection requires a concrete identity")
    lines = [f"hostname {node}", "!"]
    for name, entries in addresses.items():
        # FRR names one primary address per interface; extra /32s are secondaries.
        lines.append(f"interface {name}")
        lines.extend(f" ip address {address}" for address in entries)
        if name in shutdown:
            lines.append(" shutdown")
        lines.append("!")
    lines.extend(f"ip route {prefix} {gateway}" for prefix, gateway in routes)
    return "\n".join(lines) + "\n"
