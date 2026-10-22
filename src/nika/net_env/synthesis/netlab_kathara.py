"""Supply fixed Kathara Ethernet OSPF/BGP sketches to the upstream compiler."""

from __future__ import annotations

import re
from collections import defaultdict
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path

from nika.net_env.synthesis.netlab import run_netlab
from nika.validation.batfish.kathara import startup_addresses


def kathara_topology(env, parameters=None):
    """Preserve the topology's addresses and protocol sketch; bind solved values.

    This input adapter covers plain Ethernet OSPF areas and eBGP fabrics.
    NIKA continues to own services, Linux addressing and physical deployment.
    """
    if (
        env.backend != "kathara"
        or not env.ROUTING_PARAMETER_KINDS
        or not env.ROUTING_PARAMETER_KINDS <= {"ospf_costs", "bgp_exports"}
    ):
        raise ValueError(
            "netlab Ethernet construction requires OSPF costs or BGP export capabilities"
        )
    configs = {
        name: machine.fs.readtext("etc/frr/frr.conf")
        for name, machine in env.lab.machines.items()
        if machine.fs and machine.fs.exists("etc/frr/frr.conf")
    }
    topology = {
        "name": "nika",
        "provider": "clab",
        "defaults.device": "frr",
        "defaults.sources.list": ["package:topology-defaults.yml"],
        "defaults.const.MAX_NODE_ID_LENGTH": 63,
        "defaults.const.MAX_NODE_ID": max(150, len(configs) + 1),
        "addressing.mgmt.ipv4": "172.31.0.0/16",
        "defaults.devices.frr.clab.group_vars.netlab_mgmt_vrf": False,
        "nodes": {},
        "links": [],
    }
    links = defaultdict(list)
    sessions = set()
    costs = (
        {(p.node, p.interface): p.cost for p in parameters.ospf_costs}
        if parameters
        else {}
    )
    exports = (
        {(p.node, p.neighbor): p.terms for p in parameters.bgp_exports}
        if parameters
        else {}
    )
    has_bgp = False
    for name, config in configs.items():
        node = {"loopback": False, "module": []}
        areas = re.findall(r"^\s*network ([0-9./]+) area ([0-9.]+)$", config, re.M)
        if areas:
            (router_id,) = re.findall(
                r"^\s*(?:ospf )?router-id ([0-9.]+)$", config, re.M
            )
            node.update({"ospf.router_id": router_id})
            node["module"].append("ospf")
        peers = dict(
            re.findall(r"^\s*neighbor ([0-9.]+) remote-as ([0-9]+)$", config, re.M)
        )
        if peers:
            (asn,) = set(re.findall(r"^router bgp ([0-9]+)$", config, re.M))
            (router_id,) = re.findall(r"^\s*bgp router-id ([0-9.]+)$", config, re.M)
            node.update(
                {
                    "bgp.as": int(asn),
                    "bgp.router_id": router_id,
                    "bgp.advertise": [
                        {"ipv4": p}
                        for p in re.findall(r"^\s*network ([0-9./]+)$", config, re.M)
                    ],
                }
            )
            node["module"] += ["bgp", "routing"]
            has_bgp = True
            prefix_lists, policies = {}, {}
            for index, (peer, remote_asn) in enumerate(peers.items()):
                sessions.add((name, peer, int(remote_asn)))
                if (name, peer) not in exports:
                    continue
                entries = []
                for seq, term in enumerate(exports[name, peer]):
                    key = f"NC_PFX_{index}_{seq}"
                    prefix_lists[key] = [{"ipv4": term.prefix}]
                    entries.append(
                        {
                            "action": "permit" if term.permit else "deny",
                            "match": {"prefix": key},
                        }
                    )
                policies[f"NC_OUT_{index}"] = entries
            if policies:
                node["routing"] = {"prefix": prefix_lists, "policy": policies}
        topology["nodes"][name] = node
        startup = env.lab.fs.readtext(f"{name}.startup")
        interface = None
        configured_costs = {}
        for line in config.splitlines():
            if match := re.fullmatch(r"interface (eth[0-9]+)", line):
                interface = match[1]
            elif interface and (match := re.fullmatch(r" ip ospf cost ([0-9]+)", line)):
                configured_costs[interface] = int(match[1])
            elif line and not line[0].isspace() and not line.startswith("!"):
                interface = None
        for address, interface in startup_addresses(startup):
            endpoint = {"node": name, "ifname": interface, "ipv4": address}
            matches = {
                str(IPv4Address(int(area) if "." not in area else area))
                for prefix, area in areas
                if IPv4Interface(address).ip in IPv4Network(prefix)
            }
            if len(matches) > 1:
                raise ValueError("Overlapping OSPF area bindings are not covered")
            if matches:
                endpoint["ospf"] = {
                    "area": matches.pop(),
                    "cost": costs.get(
                        (name, interface), configured_costs.get(interface, 1)
                    ),
                    "passive": False,
                }
            elif areas:
                endpoint["ospf"] = False
            if peers:
                endpoint["bgp.advertise"] = False
                peer = next(
                    (
                        p
                        for p in peers
                        if IPv4Address(p) in IPv4Interface(address).network
                    ),
                    None,
                )
                if peer and (name, peer) in exports:
                    endpoint["bgp.policy"] = {
                        "out": f"NC_OUT_{list(peers).index(peer)}"
                    }
            if interface.startswith("eth"):
                link = env.lab.machines[name].interfaces[int(interface[3:])].link
                key = link.name
            else:
                # NIKA owns access bridges. Only their routed IPv4 endpoints
                # enter the routing sketch; L2/DHCP remain runtime concerns.
                key = (name, interface)
            links[key].append(endpoint)
    if has_bgp:
        topology.update(
            {
                "plugin": ["bgp.policy"],
                "bgp": {
                    "advertise_loopback": False,
                    "advertise_roles": [],
                    "next_hop_self": False,
                },
            }
        )
    for endpoints in links.values():
        topology["links"].append(
            {
                "type": "p2p"
                if len(endpoints) == 2
                else "stub"
                if len(endpoints) == 1
                else "lan",
                "interfaces": endpoints,
            }
        )
    if not configs:
        raise ValueError("No FRR routing sketch available")
    return topology, sessions


def generate_kathara_configs(env, parameters, directory: Path):
    topology, expected = kathara_topology(env, parameters)
    configs, model = run_netlab(topology, directory)
    actual = {
        (name, peer["ipv4"], peer["as"])
        for name, node in model["nodes"].items()
        for peer in node.get("bgp", {}).get("neighbors", [])
    }
    if actual != expected:
        raise ValueError(
            "netlab-derived BGP sessions differ from the fixed Ethernet sketch"
        )
    origins = {
        (name, p["ipv4"])
        for name, node in model["nodes"].items()
        for p in node.get("bgp", {}).get("advertise", [])
    }
    if origins != {
        (name, prefix) for prefix, name in env.bgp_origin_prefixes().items()
    }:
        raise ValueError("netlab-derived prefix origins differ from the fixed sketch")
    return configs
