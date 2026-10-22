"""Compile routing on existing tunnel endpoints while retaining local VRF imports."""

from __future__ import annotations

import re
from collections import defaultdict
from ipaddress import IPv4Interface
from pathlib import Path

from nika.net_env.synthesis.netlab import run_netlab
from nika.validation.batfish.kathara import startup_addresses


def _policies(config):
    """Read the fixed prefix-policy subset, including already bound preferences."""
    prefixes = defaultdict(list)
    for name, seq, action, prefix, maximum in re.findall(
        r"^ip prefix-list (\S+) seq ([0-9]+) (permit|deny) ([0-9./]+)(?: le ([0-9]+))?$",
        config,
        re.M,
    ):
        entry = {"ipv4": prefix, "action": action}
        if maximum:
            entry["max"] = int(maximum)
        prefixes[name].append((int(seq), entry))
    policies = defaultdict(list)
    for match in re.finditer(
        r"^route-map (\S+) (permit|deny) ([0-9]+)\n(.*?)(?=^[^\s!#]|\Z)",
        config,
        re.M | re.S,
    ):
        name, action, seq, body = match.groups()
        entry = {"action": action}
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith(("!", "#")):
                continue
            if m := re.fullmatch(r"match ip address prefix-list (\S+)", line):
                entry["match"] = {"prefix": m[1]}
            elif m := re.fullmatch(r"set local-preference ([0-9]+)", line):
                entry["set"] = {"locpref": int(m[1])}
            else:
                raise ValueError(f"Unsupported tunnel policy command: {line}")
        policies[name].append((int(seq), entry))
    return {
        "prefix": {n: [e for _, e in sorted(rows)] for n, rows in prefixes.items()},
        "policy": {n: [e for _, e in sorted(rows)] for n, rows in policies.items()},
    }


def overlay_topology(env):
    """Compile declared eBGP peers over NIKA-owned WireGuard interfaces.

    netlab's VPN route-leaking module does not express this design's imports
    between the default table and Linux business VRFs. Preserve those original
    statements; generate BGP sessions and all prefix/preference policies upstream.
    """
    peers = env.bgp_import_peers()
    if (
        env.backend != "kathara"
        or not peers
        or "import_preferences" not in env.ROUTING_PARAMETER_KINDS
    ):
        raise ValueError(
            "netlab tunnel construction requires an explicit BGP import sketch"
        )
    from nika.net_env.native_routers import router_sketches
    from nika.net_env.synthesis.netlab_kathara import native_netlab_routers

    sketches = router_sketches(env)
    configs = {name: config for name, (config, _) in sketches.items()}
    native = native_netlab_routers(env)
    topology = {
        "name": "nika",
        "provider": "clab",
        "defaults.device": "frr",
        "defaults.sources.list": ["package:topology-defaults.yml"],
        "defaults.const.MAX_NODE_ID_LENGTH": 63,
        "defaults.const.MAX_NODE_ID": max(150, len(configs) + 1),
        "addressing.mgmt.ipv4": "172.31.0.0/16",
        "defaults.devices.frr.clab.group_vars.netlab_mgmt_vrf": False,
        "plugin": ["bgp.policy"],
        "bgp": {
            "advertise_loopback": False,
            "advertise_roles": [],
            "next_hop_self": False,
        },
        "nodes": {},
        "links": [],
    }
    links = defaultdict(list)
    retained = {}
    for name, config in configs.items():
        node = {"loopback": False, "module": []}
        local_peers = [p for p in peers if p.node == name]
        if local_peers:
            (asn,) = {p.local_asn for p in local_peers}
            (router_id,) = set(re.findall(r"^ bgp router-id ([0-9.]+)$", config, re.M))
            node.update(
                {
                    "module": ["bgp", "routing"],
                    "bgp.as": asn,
                    "bgp.router_id": router_id,
                    "routing": _policies(config),
                }
            )
            # Keep the existing local VRF routing implementation, independent of
            # upstream's VPNv4-based VRF module and without changing tunnel setup.
            vrfs = re.findall(r"^vrf \S+\nexit-vrf\n", config, re.M)
            instances = re.findall(
                r"^router bgp [0-9]+ vrf \S+\n.*?(?=^router bgp |^line vty|\Z)",
                config,
                re.M | re.S,
            )
            imports = re.findall(r"^  import vrf (?!default$)(\S+)$", config, re.M)
            retained[name] = (
                "\n".join(vrfs)
                + f"\nrouter bgp {asn}\n address-family ipv4 unicast\n"
                + "".join(f"  import vrf {v}\n" for v in imports)
                + " exit-address-family\n!\n"
                + "\n".join(instances)
            )
            for peer in local_peers:
                wg = env.lab.machines[name].fs.readtext(
                    f"etc/wireguard/{peer.interface}.conf"
                )
                (address,) = re.findall(r"^Address = ([0-9./]+)$", wg, re.M)
                policy = dict(
                    (direction, policy)
                    for policy, direction in re.findall(
                        rf"^ neighbor {re.escape(peer.remote_address)} route-map (\S+) (in|out)$",
                        config,
                        re.M,
                    )
                )
                endpoint = {
                    "node": name,
                    "ifname": peer.interface,
                    "ipv4": address,
                    "bgp.advertise": False,
                    "bgp.policy": policy,
                }
                key = tuple(
                    sorted((str(IPv4Interface(address).ip), peer.remote_address))
                )
                links[key].append(endpoint)
        if name in native:
            if local_peers:
                raise ValueError("netlab overlay peers must run FRR")
            node["device"] = native[name].netlab_device
        topology["nodes"][name] = node
        startup = sketches[name][1]
        vrf_interfaces = set(
            re.findall(r"ip link set dev (eth[0-9]+) master vrf_\S+", startup)
        )
        seen = set()
        for address, interface in startup_addresses(startup):
            if interface in vrf_interfaces or interface in seen:
                continue
            seen.add(interface)
            endpoint = {
                "node": name,
                "ifname": native[name].interface(interface)
                if name in native
                else interface,
                "ipv4": address,
            }
            if local_peers:
                endpoint["bgp.advertise"] = False
            link = env.lab.machines[name].interfaces[int(interface[3:])].link
            links[link.name].append(endpoint)
    for endpoints in links.values():
        topology["links"].append(
            {"type": "p2p" if len(endpoints) == 2 else "stub", "interfaces": endpoints}
        )
    return topology, retained


def generate_overlay_configs(env, directory: Path):
    topology, retained = overlay_topology(env)
    configs, model = run_netlab(topology, directory)
    expected = {
        (p.node, p.remote_address, p.remote_asn) for p in env.bgp_import_peers()
    }
    actual = {
        (name, p["ipv4"], p["as"])
        for name, n in model["nodes"].items()
        for p in n.get("bgp", {}).get("neighbors", [])
    }
    if actual != expected:
        raise ValueError("netlab-derived tunnel peers differ from the original sketch")
    from nika.net_env.synthesis.netlab_kathara import retain_native_statements

    return retain_native_statements(
        env, {name: text + retained.get(name, "") for name, text in configs.items()}
    )
