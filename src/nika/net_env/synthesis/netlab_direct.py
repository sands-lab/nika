"""Compile existing direct eBGP vendor sketches through netlab."""

from __future__ import annotations

import re
import shlex
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path

from nika.net_env.isp.bgp.policy import BgpPolicyTerm
from nika.net_env.isp.routeros import _filter_rule
from nika.net_env.synthesis.netlab import run_netlab
from nika.net_env.synthesis.parameters import RoutingParameters


def direct_bgp_topology(env, parameters=None) -> tuple[dict, str]:
    """Build explicit links, peers, origins, and export policy for direct eBGP."""
    profile = _profile(env)
    if (
        env.backend != "kathara"
        or "bgp_exports" not in getattr(env, "ROUTING_PARAMETER_KINDS", ())
        or not hasattr(env, "BGP_ROUTERS")
    ):
        raise ValueError("Direct vendor netlab requires Kathara BGP export capability")
    routers = env.BGP_ROUTERS
    if len(routers) != 2:
        raise ValueError("Direct eBGP netlab requires exactly two routers")
    names = set(routers)
    interface_map = dict(env.BGP_INTERFACE_MAP)
    peering = env.BGP_PEERING_INTERFACE
    peer_logical = [key for key, value in interface_map.items() if value == peering]
    if len(peer_logical) != 1 or len(interface_map) != 2:
        raise ValueError("Direct BGP sketch requires one peer and one client interface")
    peer_logical = peer_logical[0]
    client_logical = next(key for key in interface_map if key != peer_logical)
    rows = {}
    for name, row in routers.items():
        link_ip = IPv4Interface(row["link_ip"])
        pc_ip = IPv4Interface(row["pc_ip"])
        peer_ip = IPv4Address(row["peer_ip"])
        rows[name] = (link_ip, pc_ip, peer_ip, int(row["as"]), int(row["peer_as"]))
    for name, (link_ip, _pc, peer_ip, asn, peer_as) in rows.items():
        remote = [node for node, data in rows.items() if data[0].ip == peer_ip]
        if len(remote) != 1:
            raise ValueError(f"Direct BGP peer on {name} does not resolve uniquely")
        remote_name = remote[0]
        remote_link, _remote_pc, remote_peer, remote_as, remote_peer_as = rows[
            remote_name
        ]
        if (
            remote_link.network != link_ip.network
            or remote_peer_as != asn
            or remote_as != peer_as
            or remote_peer != link_ip.ip
        ):
            raise ValueError(f"Direct BGP peer definitions are not reciprocal: {name}")

    origins = {
        str(IPv4Network(prefix)): node
        for prefix, node in env.bgp_origin_prefixes().items()
    }
    expected_origins = {str(row[1].network): name for name, row in rows.items()}
    if origins != expected_origins:
        raise ValueError(
            "Direct BGP origins differ from the configured client networks"
        )

    solved = RoutingParameters.model_validate(parameters or {})
    if any(values for kind, values in solved if kind != "bgp_exports"):
        raise ValueError("Direct vendor netlab supports BGP export parameters only")
    export_by_node = {}
    for policy in solved.bgp_exports:
        if policy.node not in names or policy.neighbor != str(rows[policy.node][2]):
            raise ValueError("Solved direct BGP export does not bind to the fixed peer")
        if policy.node in export_by_node:
            raise ValueError(f"Duplicate direct BGP export on {policy.node}")
        export_by_node[policy.node] = policy
    if solved.bgp_exports and set(export_by_node) != names:
        raise ValueError("Solved direct BGP exports must cover both routers")

    from nika.net_env.iosxr_simple_bgp.lab import IMAGE as IOSXR_IMAGE
    from nika.net_env.routeros_simple_bgp.lab import IMAGE as ROUTEROS_IMAGE

    device = "iosxr" if profile == "iosxr" else "routeros7"
    module = ["bgp", "routing"] if profile == "iosxr" else ["bgp"]
    topology = {
        "name": "nika-direct-bgp",
        "provider": "clab",
        "defaults.device": device,
        "defaults.sources.list": ["package:topology-defaults.yml"],
        "defaults.const.MAX_NODE_ID": 150,
        "defaults.const.MAX_NODE_ID_LENGTH": 63,
        "addressing.mgmt.ipv4": "172.31.0.0/16",
        "module": module,
        "bgp": {"advertise_loopback": False, "advertise_roles": []},
        "nodes": {},
        "links": [],
    }
    if profile == "iosxr":
        topology["plugin"] = ["bgp.policy"]

    business = sorted(origins)
    for name, (link_ip, _pc_ip, _peer_ip, asn, _peer_as) in rows.items():
        node = {
            "loopback": False,
            "bgp.as": asn,
            "bgp.router_id": str(link_ip.ip),
        }
        if profile == "iosxr":
            node["box"] = IOSXR_IMAGE
            routing_prefix = {"ORIGINS": [{"ipv4": prefix} for prefix in business]}
            policies = {"BGP-IN": [{"action": "permit"}]}
            export = export_by_node.get(name)
            terms = (
                list(export.terms)
                if export
                else [BgpPolicyTerm(prefix=prefix, permit=True) for prefix in business]
            )
            export_entries = []
            for index, term in enumerate(terms):
                prefix_set = f"NC-PREFIX-{index}"
                routing_prefix[prefix_set] = [{"ipv4": term.prefix}]
                export_entries.append(
                    {
                        "action": "permit" if term.permit else "deny",
                        "match": {"prefix": prefix_set},
                    }
                )
            policies["BGP-OUT"] = export_entries
            node["routing"] = {"prefix": routing_prefix, "policy": policies}
        else:
            node["box"] = ROUTEROS_IMAGE
            node["clab.type"] = "vr-ros"
        topology["nodes"][name] = node

    left, right = sorted(rows)
    left_row, right_row = rows[left], rows[right]
    left_endpoint = {
        "node": left,
        "ifname": peering,
        "ipv4": str(left_row[0]),
        "bgp.advertise": False,
    }
    right_endpoint = {
        "node": right,
        "ifname": peering,
        "ipv4": str(right_row[0]),
        "bgp.advertise": False,
    }
    if profile == "iosxr":
        for endpoint in (left_endpoint, right_endpoint):
            endpoint["bgp.policy"] = {"in": "BGP-IN", "out": "BGP-OUT"}
    topology["links"].append(
        {"type": "p2p", "interfaces": [left_endpoint, right_endpoint]}
    )
    for name, (link_ip, pc_ip, _peer, _asn, _peer_as) in rows.items():
        topology["links"].append(
            {
                "type": "stub",
                "interfaces": [
                    {
                        "node": name,
                        "ifname": interface_map[client_logical],
                        "ipv4": str(pc_ip),
                        "bgp.advertise": True,
                    }
                ],
            }
        )
    return topology, profile


def generate_direct_bgp_configs(env, parameters, directory: Path):
    topology, profile = direct_bgp_topology(env, parameters)
    configs, transformed = run_netlab(topology, directory)
    expected_nodes = set(env.BGP_ROUTERS)
    if (
        set(configs) != expected_nodes
        or set(transformed.get("nodes", {})) != expected_nodes
    ):
        raise ValueError("netlab direct BGP nodes differ from the original sketch")
    expected_peers = {
        (name, str(row["peer_ip"]), int(row["peer_as"]))
        for name, row in env.BGP_ROUTERS.items()
    }
    actual_peers = {
        (name, str(peer["ipv4"]), int(peer["as"]))
        for name, node in transformed["nodes"].items()
        for peer in node.get("bgp", {}).get("neighbors", [])
    }
    if actual_peers != expected_peers:
        raise ValueError("netlab direct BGP peers differ from the original sketch")
    expected_origins = {
        (node, str(IPv4Network(prefix)))
        for prefix, node in env.bgp_origin_prefixes().items()
    }
    actual_origins = {
        (name, str(prefix["ipv4"]))
        for name, node in transformed["nodes"].items()
        for prefix in node.get("bgp", {}).get("advertise", [])
        if "ipv4" in prefix
    }
    if actual_origins != expected_origins:
        raise ValueError("netlab direct BGP origins differ from the original sketch")
    _validate_interfaces(env, transformed)
    if profile == "routeros":
        configs = _routeros_exact_export_filters(env, configs, transformed, parameters)
    return configs


def _profile(env) -> str:
    models = set(getattr(env, "ROUTING_MODELS", ()))
    if models == {"iosxr-bgp"}:
        return "iosxr"
    if models == {"routeros-bgp"}:
        return "routeros"
    raise ValueError("Direct BGP environment must declare one supported vendor model")


def _validate_interfaces(env, transformed):
    expected = {}
    for name, row in env.BGP_ROUTERS.items():
        expected[name] = {
            str(IPv4Interface(row["link_ip"])): env.BGP_PEERING_INTERFACE,
            str(IPv4Interface(row["pc_ip"])): next(
                native
                for logical, native in env.BGP_INTERFACE_MAP.items()
                if native != env.BGP_PEERING_INTERFACE
            ),
        }
    for name, node in transformed["nodes"].items():
        actual = {
            str(IPv4Interface(address["ipv4"])): address["ifname"]
            for address in node.get("interfaces", [])
            if address.get("ipv4")
        }
        if actual != expected[name]:
            raise ValueError(f"netlab direct BGP addresses/interfaces differ on {name}")


def _routeros_exact_export_filters(env, configs, transformed, parameters):
    solved = RoutingParameters.model_validate(parameters or {})
    policies = {(row.node, row.neighbor): row for row in solved.bgp_exports}
    result = dict(configs)
    for node, routers in env.BGP_ROUTERS.items():
        peer = str(routers["peer_ip"])
        policy = policies.get((node, peer))
        if policy is None:
            continue
        model_peers = transformed["nodes"][node].get("bgp", {}).get("neighbors", [])
        if len(model_peers) != 1 or str(model_peers[0].get("ipv4")) != peer:
            raise ValueError(f"RouterOS transformed peer changed on {node}")
        result[node] = _bind_routeros_filter_chain(
            result[node], node, peer, policy.terms
        )
    return result


def _bind_routeros_filter_chain(config, node, peer, terms):
    matches = []
    for line in config.splitlines():
        if not line.startswith("/routing/bgp/connection add "):
            continue
        values = _attributes(line)
        if values.get("remote.address") == peer:
            matches.append(values)
    if len(matches) != 1 or not matches[0].get("name"):
        raise ValueError(f"RouterOS generated connection is ambiguous on {node}")
    connection = matches[0]["name"]
    filter_chain = f"NC-OUT-{node}-{peer.replace('.', '-')}"
    pattern = re.compile(
        rf"^(/routing/bgp/connection set \[/routing/bgp/connection find name={re.escape(connection)}\] output\.filter-chain=)\S+$",
        re.M,
    )
    old_chains = re.findall(
        rf"^/routing/bgp/connection set \[/routing/bgp/connection find name={re.escape(connection)}\] output\.filter-chain=(\S+)$",
        config,
        re.M,
    )
    if len(old_chains) != 1:
        raise ValueError(f"RouterOS generated output filter is ambiguous on {node}")
    base_chain = old_chains[0]
    config, count = pattern.subn(rf"\g<1>{filter_chain}", config)
    if count != 1:
        raise ValueError(f"RouterOS generated output filter is ambiguous on {node}")
    community_rules = [
        re.sub(
            rf"(chain=){re.escape(base_chain)}(?=\s)",
            rf"\g<1>{filter_chain}",
            line,
        )
        for line in config.splitlines()
        if f"chain={base_chain} " in line and "jump rm_" in line
    ]
    filter_rules = []
    for term in terms:
        prefix = IPv4Network(term.prefix)
        action = "accept" if term.permit else "reject"
        rule = f"if (dst == {prefix} && dst-len == {prefix.prefixlen}) {{ {action}; }}"
        filter_rules.append(_filter_rule(filter_chain, rule))
    filter_rules.append(f'/routing/filter/rule add chain={filter_chain} rule="reject;"')
    return config.rstrip() + "\n" + "\n".join([*community_rules, *filter_rules]) + "\n"


def _attributes(line):
    values = {}
    for token in shlex.split(line):
        if "=" in token:
            key, value = token.split("=", 1)
            values[key] = value
    return values
