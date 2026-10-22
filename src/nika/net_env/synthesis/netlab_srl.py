"""Compile fixed SR Linux BGP sketches with netlab's upstream policy module."""

from __future__ import annotations

from collections import defaultdict
from ipaddress import IPv4Address, IPv4Interface, IPv4Network, IPv6Interface
from pathlib import Path

import yaml

from nika.net_env.isp.containerlab.lab import SRL_IMAGE, SRL_TYPE
from nika.net_env.synthesis.netlab import run_netlab
from nika.net_env.synthesis.parameters import RoutingParameters
from nika.net_env.synthesis.srl_updates import srl_config_document


def srl_topology(env, parameters=None) -> tuple[dict, set[tuple[str, str, int]]]:
    """Build an upstream topology from existing SRL addresses and BGP peers."""
    capabilities = getattr(env, "ROUTING_PARAMETER_KINDS", frozenset())
    if (
        env.backend != "containerlab"
        or "bgp_exports" not in capabilities
        or not capabilities <= {"bgp_exports"}
        or not env.routing_documents
    ):
        raise ValueError(
            "SR Linux netlab construction requires Containerlab BGP export capability and routing documents"
        )

    solved = RoutingParameters.model_validate(parameters or {})
    if any(values for kind, values in solved if kind != "bgp_exports"):
        raise ValueError("SR Linux netlab construction supports BGP exports only")

    nodes = {}
    interfaces = {}
    ipv6_interfaces = {}
    peer_rows = defaultdict(list)
    for name, raw in env.routing_documents.items():
        document = srl_config_document(raw) if isinstance(raw, str) else raw
        try:
            (instance,) = document["srl_nokia-network-instance:network-instance"]
            (_, bgp) = next(
                (key, value)
                for key, value in instance["protocols"].items()
                if key.endswith("-bgp:bgp")
            )
            asn = int(bgp["autonomous-system"])
            router_id = str(IPv4Address(bgp["router-id"]))
            (system0,) = [
                row
                for row in document["srl_nokia-interfaces:interface"]
                if row["name"] == "system0"
            ]
            (loopback,) = system0["subinterface"]
            (loopback_address,) = loopback["ipv4"]["address"]
            loopback_ipv4 = str(IPv4Interface(loopback_address["ip-prefix"]))
            loopback_ipv6 = loopback.get("ipv6", {}).get("address", [])
            if len(loopback_ipv6) > 1:
                raise ValueError("SR Linux system0 supports one IPv6 address")
            if IPv4Interface(loopback_ipv4).ip != IPv4Address(router_id):
                raise ValueError("SR Linux BGP router ID must match system0 IPv4")
        except (KeyError, StopIteration, ValueError) as exc:
            raise ValueError(f"Unsupported SR Linux BGP sketch on {name!r}") from exc

        loopback_data = {"ipv4": loopback_ipv4}
        if loopback_ipv6:
            loopback_data["ipv6"] = str(IPv6Interface(loopback_ipv6[0]["ip-prefix"]))
        node = {
            "box": SRL_IMAGE,
            "clab.type": SRL_TYPE,
            "loopback": loopback_data,
            "module": ["bgp", "routing"],
            "bgp.as": asn,
            "bgp.router_id": router_id,
        }
        nodes[name] = node
        for row in document["srl_nokia-interfaces:interface"]:
            if row["name"] == "system0":
                continue
            (subinterface,) = row.get("subinterface", [])
            addresses = subinterface.get("ipv4", {}).get("address", [])
            if len(addresses) != 1:
                raise ValueError(
                    f"SR Linux netlab requires one IPv4 address per routed interface: {name}:{row['name']}"
                )
            address = str(IPv4Interface(addresses[0]["ip-prefix"]))
            if (name, row["name"]) in interfaces:
                raise ValueError(f"Duplicate SR Linux interface: {name}:{row['name']}")
            interfaces[name, row["name"]] = address
            ipv6_addresses = subinterface.get("ipv6", {}).get("address", [])
            if len(ipv6_addresses) > 1:
                raise ValueError(
                    f"SR Linux netlab requires one IPv6 address per interface: {name}:{row['name']}"
                )
            if ipv6_addresses:
                ipv6_interfaces[name, row["name"]] = str(
                    IPv6Interface(ipv6_addresses[0]["ip-prefix"])
                )
        for neighbor in bgp.get("neighbor", []):
            peer = str(IPv4Address(neighbor["peer-address"]))
            remote_asn = int(neighbor["peer-as"])
            if remote_asn == asn:
                raise ValueError(
                    "SR Linux netlab construction supports eBGP peers only"
                )
            peer_rows[name].append((peer, remote_asn))
        if len({peer for peer, _ in peer_rows[name]}) != len(peer_rows[name]):
            raise ValueError(f"Duplicate SR Linux BGP peer on {name!r}")

    if not nodes:
        raise ValueError("No SR Linux BGP routing documents available")

    # Match every configured peer to exactly one addressed remote interface.
    links = []
    expected = set()
    used_interfaces = set()
    built_links = set()
    for name, peers in peer_rows.items():
        for peer, remote_asn in peers:
            matches = [
                (remote_name, interface, address)
                for (remote_name, interface), address in interfaces.items()
                if remote_name != name
                and IPv4Address(peer) == IPv4Interface(address).ip
            ]
            if len(matches) != 1:
                raise ValueError(f"SR Linux peer {name}:{peer} has no unique interface")
            remote_name, remote_interface, remote_address = matches[0]
            if nodes[remote_name]["bgp.as"] != remote_asn:
                raise ValueError(f"SR Linux peer AS mismatch for {name}:{peer}")
            remote_network = IPv4Interface(remote_address).network
            local_candidates = [
                (key, address)
                for key, address in interfaces.items()
                if key[0] == name and IPv4Interface(address).network == remote_network
            ]
            if len(local_candidates) != 1:
                raise ValueError(
                    f"SR Linux peer {name}:{peer} has no unique local interface"
                )
            local_key, local_address = local_candidates[0]
            remote_peers = peer_rows[remote_name]
            reciprocal = [
                row
                for row in remote_peers
                if row[0] == str(IPv4Interface(local_address).ip)
                and row[1] == nodes[name]["bgp.as"]
            ]
            if len(reciprocal) != 1:
                raise ValueError(f"SR Linux peer {name}:{peer} is not reciprocal")
            local_interface = local_key[1]
            remote_key = (remote_name, remote_interface)
            link_key = frozenset((local_key, remote_key))
            if link_key not in built_links:
                local_endpoint = {
                    "node": name,
                    "ifname": local_interface,
                    "ipv4": interfaces[local_key],
                    "bgp.advertise": False,
                }
                remote_endpoint = {
                    "node": remote_name,
                    "ifname": remote_interface,
                    "ipv4": remote_address,
                    "bgp.advertise": False,
                }
                if local_key in ipv6_interfaces:
                    local_endpoint["ipv6"] = ipv6_interfaces[local_key]
                if remote_key in ipv6_interfaces:
                    remote_endpoint["ipv6"] = ipv6_interfaces[remote_key]
                links.append(
                    {"type": "p2p", "interfaces": [local_endpoint, remote_endpoint]}
                )
                used_interfaces.update((local_key, remote_key))
                built_links.add(link_key)
            expected.add((name, peer, remote_asn))

    origins = env.bgp_origin_prefixes()
    if not origins:
        raise ValueError("SR Linux BGP sketch has no declared origin prefixes")
    for prefix, owner in origins.items():
        network = IPv4Network(prefix, strict=False)
        if owner not in nodes:
            raise ValueError(f"Unknown SR Linux origin node: {owner}")
        matching = [
            key
            for key, address in interfaces.items()
            if key[0] == owner and IPv4Interface(address).network == network
        ]
        if len(matching) != 1:
            raise ValueError(
                f"Origin {prefix} does not match one fixed interface on {owner}"
            )
        nodes[owner].setdefault("bgp.advertise", []).append({"ipv4": str(network)})

    for key, address in interfaces.items():
        if key in used_interfaces:
            continue
        endpoint = {
            "node": key[0],
            "ifname": key[1],
            "ipv4": address,
            "bgp.advertise": False,
        }
        if key in ipv6_interfaces:
            endpoint["ipv6"] = ipv6_interfaces[key]
        links.append(
            {
                "type": "stub",
                "interfaces": [endpoint],
            }
        )

    export_map = {(item.node, item.neighbor): item.terms for item in solved.bgp_exports}
    if export_map and set(export_map) != {(node, peer) for node, peer, _ in expected}:
        raise ValueError("Solved SR Linux exports must cover every fixed BGP peer")
    for index, ((node_name, neighbor), terms) in enumerate(sorted(export_map.items())):
        prefixes, statements = {}, []
        for seq, term in enumerate(terms, 1):
            prefix_set = f"NC_PFX_{index}_{seq}"
            prefixes[prefix_set] = [{"ipv4": term.prefix}]
            statements.append(
                {
                    "action": "permit" if term.permit else "deny",
                    "match": {"prefix": prefix_set},
                }
            )
        statements.append({"action": "deny"})
        node = nodes[node_name]
        node.setdefault("routing", {"prefix": {}, "policy": {}})
        node["routing"]["prefix"].update(prefixes)
        policy = f"NC_OUT_{index}"
        node["routing"]["policy"][policy] = statements
        for link in links:
            for endpoint in link["interfaces"]:
                if endpoint["node"] != node_name:
                    continue
                peer_endpoint = next(
                    (
                        candidate
                        for candidate in link["interfaces"]
                        if candidate["node"] != node_name
                    ),
                    None,
                )
                if (
                    peer_endpoint
                    and str(IPv4Interface(peer_endpoint["ipv4"]).ip) == neighbor
                ):
                    endpoint["bgp.policy"] = {"out": policy}

    topology = {
        "name": "nika-srl-bgp",
        "provider": "clab",
        "defaults.device": "srlinux",
        "defaults.sources.list": ["package:topology-defaults.yml"],
        "defaults.const.MAX_NODE_ID_LENGTH": 63,
        "defaults.const.MAX_NODE_ID": max(150, len(nodes) + 1),
        "addressing.mgmt.ipv4": "172.31.0.0/16",
        "plugin": ["bgp.policy"],
        "bgp": {
            "advertise_loopback": False,
            "advertise_roles": [],
            "next_hop_self": False,
            "sessions": {"ipv4": ["ebgp"], "ipv6": []},
            "activate": {"ipv4": ["ebgp"], "ipv6": []},
        },
        "nodes": nodes,
        "links": links,
    }
    return topology, expected


def generate_srl_configs(env, parameters, directory: Path) -> dict[str, str]:
    """Compile SR Linux BGP completion to per-node netlab operation YAML."""
    topology, expected = srl_topology(env, parameters)
    # The pinned upstream SRL BGP template repeats its YAML afi-safi key when
    # both node families are present. Keep the IPv4 routing scope explicit and
    # retain the original IPv6 interface configuration without enabling IPv6 BGP.
    for node in topology["nodes"].values():
        node["loopback"].pop("ipv6", None)
    for link in topology["links"]:
        for endpoint in link["interfaces"]:
            endpoint.pop("ipv6", None)
    configs, model = run_netlab(topology, directory)
    actual = {
        (name, peer["ipv4"], peer["as"])
        for name, node in model["nodes"].items()
        for peer in node.get("bgp", {}).get("neighbors", [])
    }
    if actual != expected:
        raise ValueError("netlab-derived SR Linux peers differ from the fixed sketch")
    origins = {
        (name, prefix["ipv4"])
        for name, node in model["nodes"].items()
        for prefix in node.get("bgp", {}).get("advertise", [])
    }
    if origins != {
        (node, prefix) for prefix, node in env.bgp_origin_prefixes().items()
    }:
        raise ValueError("netlab-derived SR Linux origins differ from the fixed sketch")
    expected_interfaces = {
        (endpoint["node"], endpoint["ifname"], endpoint["ipv4"], endpoint.get("ipv6"))
        for link in topology["links"]
        for endpoint in link["interfaces"]
    }
    actual_interfaces = {
        (name, interface["ifname"], interface["ipv4"], interface.get("ipv6"))
        for name, node in model["nodes"].items()
        for interface in node.get("interfaces", [])
    }
    if actual_interfaces != expected_interfaces:
        raise ValueError(
            "netlab-derived SR Linux interface addresses differ from the fixed sketch"
        )
    expected_loopbacks = {
        (name, node["loopback"]["ipv4"], node["loopback"].get("ipv6"))
        for name, node in topology["nodes"].items()
    }
    actual_loopbacks = {
        (name, node["loopback"]["ipv4"], node["loopback"].get("ipv6"))
        for name, node in model["nodes"].items()
    }
    if actual_loopbacks != expected_loopbacks:
        raise ValueError(
            "netlab-derived SR Linux system0 addresses differ from the fixed sketch"
        )
    return retain_srl_attributes(configs, env.routing_documents)


def retain_srl_attributes(configs, documents):
    """Keep existing attributes unavailable in the pinned routing templates.

    The IPv4 routing generator owns peers, origins and policies. Preserve the
    sketch's IPv6 interface addressing and explicit IGP ECMP capacity; neither
    requires a second protocol or policy renderer.
    """
    for name, text in configs.items():
        output = yaml.safe_load(text)
        for interface in documents[name]["srl_nokia-interfaces:interface"]:
            for sub in interface["subinterface"]:
                if "ipv6" in sub:
                    output["operations"].append(
                        {
                            "operation": "update",
                            "path": f"/interface[name={interface['name']}]/subinterface[index={sub['index']}]",
                            "value": {"ipv6": sub["ipv6"]},
                        }
                    )
        for instance in documents[name]["srl_nokia-network-instance:network-instance"]:
            for protocol in ("isis", "ospf"):
                value = instance.get("protocols", {}).get(
                    f"srl_nokia-{protocol}:{protocol}", {}
                )
                for igp in value.get("instance", []):
                    if "max-ecmp-paths" in igp:
                        # The pinned OSPF template names its process 1.
                        process_name = "1" if protocol == "ospf" else igp["name"]
                        output["operations"].append(
                            {
                                "operation": "update",
                                "path": f"/network-instance[name={instance['name']}]/protocols/{protocol}/instance[name={process_name}]",
                                "value": {"max-ecmp-paths": igp["max-ecmp-paths"]},
                            }
                        )
        configs[name] = yaml.safe_dump(output, sort_keys=False)
    return configs
