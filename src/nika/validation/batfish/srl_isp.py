"""Restricted concrete SR Linux IPv4 IGP/BGP projection for ISP experiments."""

from __future__ import annotations

import json
import re
from ipaddress import IPv4Interface, IPv4Network

import yaml

from nika.net_env.isp.igp.frr import ISIS_PROCESS
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.snapshot import prepare_snapshot, _edge


def _keys(row, required, optional=()):
    if not set(required) <= row.keys() or set(row) - set(required) - set(optional):
        raise ValueError(f"Unsupported SR Linux attributes: {row}")


def canonical_interface(name):
    if name == "system0.0":
        return "lo"
    if name.startswith("lo0."):
        return "lo" + name.split(".")[-1]
    match = re.fullmatch(r"ethernet-1/([0-9]+)\.0", name)
    if not match:
        raise ValueError(f"Unsupported SR Linux interface {name}")
    return "eth" + str(int(match[1]) - 1)


def native_interface(name):
    if name.startswith("system0.") or name.startswith("lo0."):
        return "Loopback" + name.split(".")[-1]
    match = re.fullmatch(r"ethernet-1/([0-9]+)\.0", name)
    if not match:
        raise ValueError(f"Unsupported SR Linux interface {name}")
    return f"GigabitEthernet0/0/0/{int(match[1]) - 1}"


def srl_addresses(document):
    return [
        (row["ip-prefix"], f"{i['name']}.{sub['index']}")
        for i in document["srl_nokia-interfaces:interface"]
        for sub in i["subinterface"]
        for row in sub["ipv4"]["address"]
    ]


def project_srl_isp(node, document, *, shutdown=(), require_igp=True):
    from nika.validation.batfish.srl_netlab import normalize_netlab_srl

    document = normalize_netlab_srl(document)
    origins = document.pop("_netlab_origins", None)
    _keys(
        document,
        [
            "srl_nokia-interfaces:interface",
            "srl_nokia-network-instance:network-instance",
        ],
        ["srl_nokia-routing-policy:routing-policy"],
    )
    (instance,) = document["srl_nokia-network-instance:network-instance"]
    _keys(instance, ["name", "admin-state", "interface", "protocols"])
    if instance["name"] != "default" or instance["admin-state"] != "enable":
        raise ValueError("Projection requires enabled default instance")
    attached = {r["name"] for r in instance["interface"]}
    # OSPF-only output shares IOS tokens; select the intended XR grammar.
    lines = ["!! IOS XR Configuration 7.0.0", f"hostname {node}"]
    interfaces = {}
    addresses = {}
    for interface in document["srl_nokia-interfaces:interface"]:
        _keys(interface, ["name", "admin-state", "subinterface"])
        if interface["admin-state"] != "enable":
            raise ValueError("Disabled interfaces require explicit outage input")
        for sub in interface["subinterface"]:
            _keys(sub, ["index", "ipv4"], ["admin-state"])
            _keys(sub["ipv4"], ["admin-state", "address"])
            if sub.get("admin-state", "enable") != "enable":
                raise ValueError("Projection requires enabled subinterfaces")
            if sub["ipv4"]["admin-state"] != "enable":
                raise ValueError("Projection requires enabled IPv4")
            name = f"{interface['name']}.{sub['index']}"
            if name not in attached or name in interfaces:
                raise ValueError("Unattached or duplicate interface")
            interfaces[name] = native_interface(name)
            lines.append(f"interface {interfaces[name]}")
            for row in sub["ipv4"]["address"]:
                _keys(row, ["ip-prefix"])
                addr = IPv4Interface(row["ip-prefix"])
                if str(addr.ip) in addresses:
                    raise ValueError("Duplicate IPv4 address")
                addresses[str(addr.ip)] = (name, addr)
                lines.append(f" ipv4 address {addr}")
            endpoint = interface["name"].replace("ethernet-", "e").replace("/", "-")
            if (node, endpoint) in shutdown:
                lines.append(" shutdown")
            lines.append("!")
    if attached != set(interfaces):
        raise ValueError("Unmodeled attached interfaces")
    protocols = instance["protocols"]
    igps = set(protocols) & {"srl_nokia-isis:isis", "srl_nokia-ospf:ospf"}
    if (
        (require_igp and len(igps) != 1)
        or len(igps) > 1
        or set(protocols) - igps - {"srl_nokia-bgp:bgp"}
    ):
        raise ValueError("Projection requires one supported IGP")
    if igps:
        key = next(iter(igps))
        (igp,) = protocols[key]["instance"]
        if "isis" in key:
            _keys(
                igp,
                [
                    "name",
                    "admin-state",
                    "level-capability",
                    "net",
                    "interface",
                    "level",
                ],
                ["max-ecmp-paths"],
            )
            if (
                igp["name"],
                igp["admin-state"],
                igp["level-capability"],
                igp["level"],
            ) != (
                "default",
                "enable",
                "L2",
                [{"level-number": 2, "metric-style": "wide"}],
            ):
                raise ValueError("Projection requires single L2 wide-metric IS-IS")
            (net,) = igp["net"]
            lines += [
                f"router isis {ISIS_PROCESS}",
                f" net {net}",
                " is-type level-2-only",
                " address-family ipv4 unicast",
                "  metric-style wide",
                " !",
            ]
            rows = igp["interface"]
            for row in rows:
                _keys(row, ["interface-name"], ["passive", "circuit-type", "level"])
                lines.append(f" interface {interfaces[row['interface-name']]}")
                if row.get("circuit-type") not in (None, "point-to-point"):
                    raise ValueError("Unsupported IS-IS circuit type")
                if row.get("passive"):
                    lines.append("  passive")
                if row.get("circuit-type"):
                    lines.append("  point-to-point")
                lines += ["  address-family ipv4 unicast"]
                if "level" in row:
                    (level,) = row["level"]
                    _keys(level, ["level-number", "metric"])
                    if (
                        level["level-number"] != 2
                        or not 1 <= level["metric"] <= 16777214
                    ):
                        raise ValueError("Unsupported IS-IS metric")
                    lines.append(f"   metric {level['metric']}")
                lines += ["  !", " !"]
        else:
            _keys(
                igp,
                ["name", "admin-state", "version", "router-id", "area"],
                ["max-ecmp-paths"],
            )
            if igp["name"] not in {"default", "1"} or (
                igp["admin-state"],
                igp["version"],
            ) != (
                "enable",
                "ospf-v2",
            ):
                raise ValueError("Projection requires OSPFv2")
            (area,) = igp["area"]
            _keys(area, ["area-id", "interface"])
            lines += [
                "router ospf",
                f" router-id {igp['router-id']}",
                f" area {area['area-id']}",
            ]
            for row in area["interface"]:
                _keys(row, ["interface-name"], ["passive", "metric", "interface-type"])
                lines.append(f"  interface {interfaces[row['interface-name']]}")
                if row.get("passive"):
                    lines.append("   passive enable")
                if "metric" in row:
                    lines.append(f"   cost {row['metric']}")
                if row.get("interface-type"):
                    if row["interface-type"] != "point-to-point":
                        raise ValueError("Unsupported OSPF type")
                    lines.append("   network point-to-point")
                lines.append("  !")
            lines.append(" !")
        lines.append("!")
    bgp = protocols.get("srl_nokia-bgp:bgp")
    if bgp:
        lines += _project_bgp(
            bgp,
            document["srl_nokia-routing-policy:routing-policy"],
            addresses,
            origins=origins,
        )
    return "\n".join(lines) + "\n"


def _project_bgp(bgp, routing, addresses, *, origins=None):
    _keys(
        bgp,
        [
            "admin-state",
            "autonomous-system",
            "router-id",
            "afi-safi",
            "group",
            "neighbor",
        ],
        ["route-reflector", "ebgp-default-policy"],
    )
    family = [{"afi-safi-name": "ipv4-unicast", "admin-state": "enable"}]
    if bgp["admin-state"] != "enable" or bgp["afi-safi"] != family:
        raise ValueError("Projection requires IPv4 unicast BGP")
    if "ebgp-default-policy" in bgp and bgp["ebgp-default-policy"] != {
        "import-reject-all": False,
        "export-reject-all": False,
    }:
        raise ValueError("Unsupported default BGP policy")
    _keys(routing, ["prefix-set", "policy"])
    lines = []
    sets = set()
    policies = set()
    for row in routing["prefix-set"]:
        _keys(row, ["name", "prefix"])
        name = row["name"]
        sets.add(name)
        entries = []
        for p in row["prefix"]:
            _keys(p, ["ip-prefix", "mask-length-range"])
            prefix = IPv4Network(p["ip-prefix"])
            if p["mask-length-range"] not in {
                "exact",
                f"{prefix.prefixlen}..{prefix.prefixlen}",
            }:
                raise ValueError("Only exact prefix sets supported")
            entries.append(" " + str(prefix))
        lines += [f"prefix-set {name}", ",\n".join(entries), "end-set", "!"]
    for row in routing["policy"]:
        _keys(row, ["name", "statement"])
        policies.add(row["name"])
        lines.append(f"route-policy {row['name']}")
        covered_prefix_sets = set()
        for statement in row["statement"]:
            _keys(statement, ["name", "action"], ["match"])
            _keys(statement["action"], ["policy-result"], ["bgp"])
            action = statement["action"]["policy-result"]
            if action not in {"accept", "reject", "next-statement"}:
                raise ValueError("Unsupported policy action")
            match = statement.get("match", {})
            _keys(match, [], ["prefix-set", "protocol"])
            tests = []
            if "prefix-set" in match:
                if match["prefix-set"] not in sets:
                    raise ValueError("Undefined prefix set")
                tests.append("destination in " + match["prefix-set"])
            if "protocol" in match:
                if (
                    match["protocol"] != "bgp"
                    or match.get("prefix-set") not in covered_prefix_sets
                ):
                    raise ValueError(
                        "Non-shadowed protocol-qualified SR Linux policy is outside the projection"
                    )
                # A previous terminating prefix-only clause already handles every
                # route this clause can match. Preserve first-match semantics.
                continue
            if "prefix-set" in match and action != "next-statement":
                covered_prefix_sets.add(match["prefix-set"])
            if tests:
                lines.append(" if " + " and ".join(tests) + " then")
            if attributes := statement["action"].get("bgp"):
                _keys(attributes, ["local-preference"])
                _keys(attributes["local-preference"], ["set"])
                value = attributes["local-preference"]["set"]
                if type(value) is not int or not 1 <= value <= 4294967295:
                    raise ValueError("Unsupported SR Linux local preference")
                lines.append(f"  set local-preference {value}")
            if action != "next-statement":
                lines.append("  done" if action == "accept" else "  drop")
            if tests:
                lines.append(" endif")
        lines += [" drop", "end-policy", "!"]
    groups = {}
    for group in bgp["group"]:
        _keys(group, ["group-name", "admin-state", "afi-safi"], ["peer-as"])
        (af,) = group["afi-safi"]
        _keys(af, ["afi-safi-name", "admin-state", "import-policy", "export-policy"])
        if (
            group["admin-state"] != "enable"
            or af["afi-safi-name"] != "ipv4-unicast"
            or af["admin-state"] != "enable"
        ):
            raise ValueError("Unsupported inherited BGP group")
        groups[group["group-name"]] = af
    lines += [
        f"router bgp {bgp['autonomous-system']}",
        f" bgp router-id {bgp['router-id']}",
    ]
    if "route-reflector" in bgp:
        _keys(bgp["route-reflector"], ["cluster-id"])
        lines.append(" bgp cluster-id " + bgp["route-reflector"]["cluster-id"])
    lines.append(" address-family ipv4 unicast")
    for name, addr in addresses.values():
        if (origins is not None and str(addr.network) in origins) or (
            origins is None and name.startswith("lo0.") and addr.network.prefixlen < 32
        ):
            lines.append("  network " + str(addr.network))
    lines.append(" !")
    for peer in bgp["neighbor"]:
        _keys(
            peer,
            ["peer-address", "peer-as", "peer-group", "transport"],
            ["route-reflector", "afi-safi"],
        )
        _keys(peer["transport"], ["local-address"])
        source = addresses[peer["transport"]["local-address"]][0]
        lines += [
            f" neighbor {peer['peer-address']}",
            f"  remote-as {peer['peer-as']}",
            f"  update-source {native_interface(source)}",
            "  address-family ipv4 unicast",
        ]
        if "route-reflector" in peer:
            _keys(peer["route-reflector"], ["client"])
            if peer["route-reflector"]["client"]:
                lines.append("   route-reflector-client")
        af = groups[peer["peer-group"]]
        override = {}
        if "afi-safi" in peer:
            (override,) = peer["afi-safi"]
            _keys(
                override,
                ["afi-safi-name", "admin-state"],
                ["import-policy", "export-policy"],
            )
            if (override["afi-safi-name"], override["admin-state"]) != (
                "ipv4-unicast",
                "enable",
            ):
                raise ValueError("Unsupported neighbor address family")
        for direction in ["import", "export"]:
            (policy,) = override.get(direction + "-policy", af[direction + "-policy"])
            if policy not in policies:
                raise ValueError("Undefined BGP policy")
            lines.append(
                f"   route-policy {policy} {'in' if direction == 'import' else 'out'}"
            )
        lines += ["  !", " !"]
    return [*lines, "!"]


def build_srl_isp_snapshot(env, contract, root, *, configs=None, shutdown=()):
    documents = (
        env.routing_documents
        if configs is None
        else {n: yaml.safe_load(c) for n, c in configs.items()}
    )
    if set(documents) != {n.device_name for n in env.plan.nodes}:
        raise ValueError("Incomplete SR Linux candidate")
    for intent in contract.intents:
        if intent.route_next_hops:
            expected = intent.route_next_hops
            protocols = documents[expected.node][
                "srl_nokia-network-instance:network-instance"
            ][0]["protocols"]
            igp = next(
                v
                for k, v in protocols.items()
                if k in {"srl_nokia-isis:isis", "srl_nokia-ospf:ospf"}
            )["instance"][0]
            if len(expected.next_hops) > igp.get("max-ecmp-paths", 1):
                raise ValueError(
                    "Declared ECMP set exceeds the concrete SR Linux capacity"
                )
    root = prepare_snapshot(root)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(exist_ok=True)
    aliases = {}
    for node, doc in documents.items():
        (root / "configs" / f"{node}.cfg").write_text(
            project_srl_isp(node, doc, shutdown=shutdown)
        )
        aliases[node] = {
            native_interface(name): canonical_interface(name)
            for _, name in srl_addresses(doc)
        }
    edges = []
    for link in env.plan.links:
        edges.append(
            _edge(
                link.endpoint_a,
                native_interface(f"ethernet-1/{int(link.iface_a[3:]) + 1}.0"),
                link.endpoint_b,
                native_interface(f"ethernet-1/{int(link.iface_b[3:]) + 1}.0"),
            )
        )
    (root / "batfish" / "layer1_topology.json").write_text(
        json.dumps({"edges": edges}) + "\n"
    )
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=root,
        metadata={
            "routing_model": "srl-isp",
            "native_config_analysis": False,
            "interface_aliases": aliases,
            "scope": "Concrete default-instance IPv4 OSPFv2 or IS-IS L2, BGP/RR, exact-prefix admission and local-preference policies",
        },
    )
