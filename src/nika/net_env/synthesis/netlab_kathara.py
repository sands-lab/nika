"""Supply fixed Kathara Ethernet OSPF/BGP sketches to the upstream compiler."""

from __future__ import annotations

from collections import defaultdict
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path

from nika.net_env.synthesis.configuration import RouterConfigs
from nika.net_env.synthesis.netlab import run_netlab
from nika.net_env.synthesis.parameters import RoutingParameters


def native_netlab_routers(env) -> dict:
    """Return the validation projection of each router netlab generates natively."""
    from nika.net_env.native_routers import native_platform

    native = {}
    for name, profile in getattr(env, "native_routers", {}).items():
        validation = native_platform(profile).validation
        if validation is None or validation.netlab_device is None:
            raise ValueError(f"netlab cannot generate {profile} router configuration")
        native[name] = validation
    return native


def netlab_base_topology(node_count: int) -> dict:
    """Return the netlab defaults shared by Kathara Ethernet constructions."""
    return {
        "name": "nika",
        "provider": "clab",
        "defaults.device": "frr",
        "defaults.sources.list": ["package:topology-defaults.yml"],
        "defaults.const.MAX_NODE_ID_LENGTH": 63,
        "defaults.const.MAX_NODE_ID": max(150, node_count + 1),
        "addressing.mgmt.ipv4": "172.31.0.0/16",
        "defaults.devices.frr.clab.group_vars.netlab_mgmt_vrf": False,
        # NIKA's FRR routers run FRR's default (traditional) profile.
        "defaults.devices.frr.clab.group_vars.netlab_frr_defaults": "traditional",
        "nodes": {},
        "links": [],
    }


def routing_design(env):
    design = getattr(env, "routing_design", None)
    if design is None or not design.routers:
        raise ValueError(
            "netlab construction requires the environment's routing design"
        )
    return design


def _bgp_policies(router, solved):
    """Return the router's prefix lists and policies with solved values bound."""
    prefix_lists = {
        name: [
            {"ipv4": e.prefix, "action": "permit" if e.permit else "deny"}
            | ({"max": e.le} if e.le is not None else {})
            for e in entries
        ]
        for name, entries in router.prefix_lists.items()
    }
    policies = {
        name: [
            {"action": "permit" if e.permit else "deny"}
            | (
                {"match": {"prefix": e.match_prefix_list}}
                if e.match_prefix_list
                else {}
            )
            | (
                {"set": {"locpref": e.local_preference}}
                if e.local_preference is not None
                else {}
            )
            for e in entries
        ]
        for name, entries in router.policies.items()
    }
    bindings = {
        p.address: {"in": p.import_policy, "out": p.export_policy}
        for p in router.neighbors
    }
    for index, peer in enumerate(router.neighbors):
        terms = [
            t
            for e in solved.bgp_exports
            if (e.node, e.neighbor) == (router.name, peer.address)
            for t in e.terms
        ]
        if not terms:
            continue
        if peer.export_policy:
            raise ValueError("Completion cannot replace an existing export policy")
        policy = []
        for seq, term in enumerate(terms):
            key = f"NC_PFX_{index}_{seq}"
            prefix_lists[key] = [{"ipv4": term.prefix}]
            policy.append(
                {
                    "action": "permit" if term.permit else "deny",
                    "match": {"prefix": key},
                }
            )
        policies[f"NC_OUT_{index}"] = policy
        bindings[peer.address]["out"] = f"NC_OUT_{index}"
    preferences = defaultdict(list)
    for p in solved.import_preferences:
        if p.node == router.name:
            preferences[p.interface].append(p)
    for interface, solved_terms in sorted(preferences.items()):
        (peer,) = [p for p in router.neighbors if p.interface == interface]
        policy = policies.get(peer.import_policy)
        if not policy or sum("set" in e for e in policy) != 1:
            raise ValueError("Expected one existing tunnel import preference")
        if any(t.prefix is None for t in solved_terms):
            if len(solved_terms) != 1:
                raise ValueError(
                    "Cannot mix prefix-specific and whole-interface imports"
                )
            for entry in policy:
                if "set" in entry:
                    entry["set"] = {"locpref": solved_terms[0].local_preference}
            continue
        # Exact-prefix rules precede the designed policy, which keeps
        # prefixes outside the declared completion scope.
        exact = []
        for seq, term in enumerate(sorted(solved_terms, key=lambda t: t.prefix), 1):
            name = f"NC-IN-{interface}-{seq}"
            prefix_lists[name] = [{"ipv4": term.prefix, "action": "permit"}]
            entry = {
                "action": "permit" if term.permit else "deny",
                "match": {"prefix": name},
            }
            if term.permit:
                entry["set"] = {"locpref": term.local_preference}
            exact.append(entry)
        policies[peer.import_policy] = exact + policy
    return prefix_lists, policies, bindings


def kathara_topology(env, parameters=None, *, as_frr=False):
    """Compile the recorded routing design with the solved values.

    This input adapter covers Ethernet OSPF areas, eBGP fabrics and eBGP over
    tunnel interfaces. NIKA continues to own services, Linux addressing, VRFs
    and physical deployment. With ``as_frr``, every router is compiled as FRR.
    """
    if (
        env.backend != "kathara"
        or not env.ROUTING_PARAMETER_KINDS
        or not env.ROUTING_PARAMETER_KINDS
        <= {"ospf_costs", "bgp_exports", "import_preferences"}
    ):
        raise ValueError(
            "netlab Ethernet construction requires OSPF cost or BGP policy capabilities"
        )
    design = routing_design(env)
    native = {} if as_frr else native_netlab_routers(env)
    solved = RoutingParameters.model_validate(parameters or {})
    costs = {(p.node, p.interface): p.cost for p in solved.ospf_costs}
    topology = netlab_base_topology(len(design.routers))
    links = defaultdict(list)
    tunnels = {}
    sessions = set()
    has_bgp = False
    for router in design.routers.values():
        name = router.name
        node = {"loopback": False, "module": []}
        if router.runs_ospf:
            node.update({"ospf.router_id": router.router_id})
            node["module"].append("ospf")
        bindings = {}
        external = any(p.remote_as != router.asn for p in router.neighbors)
        if external and router.ebgp_requires_policy and name not in native:
            # The pinned FRR template always disables RFC 8212 enforcement.
            raise ValueError(
                f"netlab cannot generate {name}: its design requires eBGP policies"
            )
        if router.neighbors:
            node.update(
                {
                    "bgp.as": router.asn,
                    "bgp.router_id": router.router_id,
                    "bgp.advertise": [{"ipv4": p} for p in router.bgp_networks],
                }
            )
            node["module"] += ["bgp", "routing"]
            has_bgp = True
            sessions |= {(name, p.address, p.remote_as) for p in router.neighbors}
            prefix_lists, policies, bindings = _bgp_policies(router, solved)
            if policies:
                node["routing"] = {"prefix": prefix_lists, "policy": policies}
        if router.debug and name not in native:
            node["frr.debug"] = list(router.debug)
        if name in native:
            node["device"] = native[name].netlab_device
            if router.static_routes:
                # FRR routers keep these routes in their Linux startup.
                if "routing" not in node["module"]:
                    node["module"].append("routing")
                node["routing.static"] = [
                    {"ipv4": prefix, "nexthop": {"ipv4": via}}
                    for prefix, via in router.static_routes
                ]
        topology["nodes"][name] = node
        for interface in router.interfaces:
            if interface.vrf is not None:
                # Business VRFs stay with NIKA; see the retained statements.
                continue
            endpoint = {
                "node": name,
                "ifname": native[name].interface(interface.name)
                if name in native
                else interface.name,
                "ipv4": interface.address,
            }
            area = router.ospf_area(interface)
            if area is not None:
                endpoint["ospf"] = {"area": area, "passive": False}
                cost = costs.get((name, interface.name))
                # Without a solved cost, keep FRR's bandwidth-derived default,
                # as the native configuration does.
                if cost is not None:
                    endpoint["ospf"]["cost"] = cost
            elif router.runs_ospf:
                endpoint["ospf"] = False
            if router.neighbors:
                endpoint["bgp.advertise"] = False
                network = IPv4Interface(interface.address).network
                peer = next(
                    (
                        p
                        for p in router.neighbors
                        if p.interface == interface.name
                        or (p.interface is None and IPv4Address(p.address) in network)
                    ),
                    None,
                )
                if peer is not None:
                    policy = {k: v for k, v in bindings[peer.address].items() if v}
                    if policy:
                        endpoint["bgp.policy"] = policy
            # NIKA owns access bridges. Only their routed IPv4 endpoints enter
            # the routing sketch; L2/DHCP remain runtime concerns.
            key = interface.link if interface.link else (name, interface.name)
            links[key].append(endpoint)
            wireguard = interface.wireguard
            if wireguard is not None and name not in native:
                # netlab's tunnel plugin derives the peer endpoint from the
                # remote tunnel source, selected below by its link ID.
                endpoint["tunnel"] = {
                    "source": wireguard.source,
                    "private_key": wireguard.private_key,
                    "public_key": wireguard.public_key,
                    "listen_port": wireguard.listen_port,
                }
                tunnels[key] = {
                    "type": "tunnel",
                    "tunnel": {
                        "mode": "wireguard",
                        "allowed_ips": wireguard.allowed_ips,
                        "persistent_keepalive": wireguard.keepalive,
                    },
                    "mtu": wireguard.mtu,
                }
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
    for key, endpoints in links.items():
        link = {
            "type": "p2p"
            if len(endpoints) == 2
            else "stub"
            if len(endpoints) == 1
            else "lan",
            "interfaces": endpoints,
        }
        if key in tunnels:
            if not all("tunnel" in endpoint for endpoint in endpoints):
                raise ValueError(f"WireGuard tunnel {key} needs two FRR endpoints")
            link.update(tunnels[key])
        topology["links"].append(link)
    if tunnels:
        topology["plugin"] = [*topology.get("plugin", []), "tunnel.wireguard"]
        link_ids = {}
        for index, link in enumerate(topology["links"], start=1):
            for endpoint in link["interfaces"]:
                link_ids[(endpoint["node"], endpoint["ifname"])] = (link, index)
        for link in topology["links"]:
            for endpoint in link["interfaces"]:
                if "tunnel" in endpoint:
                    source, index = link_ids[
                        (endpoint["node"], endpoint["tunnel"]["source"])
                    ]
                    source["linkid"] = f"link{index}"
                    endpoint["tunnel"]["source"] = {"linkid": f"link{index}"}
    return topology, sessions


def generate_kathara_configs(env, parameters, directory: Path, *, as_frr=False):
    """Generate every router's configuration from the recorded design.

    With ``as_frr``, every router is generated as FRR; static analysis uses
    this rendering for routers that Batfish cannot parse.
    """
    topology, expected = kathara_topology(env, parameters, as_frr=as_frr)
    configs, model = run_netlab(topology, directory)
    actual = {
        (name, peer["ipv4"], peer["as"])
        for name, node in model["nodes"].items()
        for peer in node.get("bgp", {}).get("neighbors", [])
    }
    if actual != expected:
        raise ValueError("netlab-derived BGP sessions differ from the routing design")
    origins = {
        (name, p["ipv4"])
        for name, node in model["nodes"].items()
        for p in node.get("bgp", {}).get("advertise", [])
    }
    design = routing_design(env)
    if origins != {(n, p) for p, n in design.origin_prefixes().items()}:
        raise ValueError("netlab-derived prefix origins differ from the routing design")
    _check_tunnel_endpoints(design, model)
    solved = RoutingParameters.model_validate(parameters or {})
    native = {} if as_frr else native_netlab_routers(env)
    result = RouterConfigs(scripts=configs.scripts)
    for name, text in configs.items():
        router = design.routers[name]
        text += "".join(r.text for r in router.retained)
        if name not in native and router.bgp_multipath and router.neighbors:
            text += FRR_MULTIPATH.format(asn=router.asn, paths=router.bgp_multipath)
        if name in native and native[name].netlab_retained is not None:
            _, _, bindings = _bgp_policies(router, solved)
            bound = {
                (peer, direction)
                for peer, policy in bindings.items()
                for direction, value in policy.items()
                if value
            }
            text += native[name].netlab_retained(router, bound)
        result[name] = text
    return result


def _check_tunnel_endpoints(design, model) -> None:
    """Reject tunnels whose netlab endpoints differ from the design's sources."""
    sources = {
        (router.name, interface.name): router.interface(
            interface.wireguard.source
        ).address.split("/")[0]
        for router in design.routers.values()
        for interface in router.interfaces
        if interface.wireguard is not None
    }
    for name, node in model["nodes"].items():
        for interface in node.get("interfaces", []):
            tunnel = interface.get("tunnel")
            if not tunnel or (name, interface["ifname"]) not in sources:
                continue
            peer = next(p["node"] for p in interface["neighbors"])
            expected = (
                sources[(name, interface["ifname"])],
                sources[(peer, interface["neighbors"][0]["ifname"])],
            )
            actual = (tunnel["_source"]["ipv4"], tunnel["_destination"]["ipv4"])
            if actual != expected:
                raise ValueError(
                    f"netlab tunnel {name}:{interface['ifname']} endpoints {actual} "
                    f"differ from the design {expected}"
                )


# The pinned netlab FRR templates set no BGP maximum-paths.
FRR_MULTIPATH = (
    "router bgp {asn}\n address-family ipv4 unicast\n  maximum-paths {paths}\n"
    " exit-address-family\n!\n"
)


def retained_capabilities(env) -> list[dict[str, str]]:
    """List each capability NIKA renders beside netlab output, with the reason."""
    design = routing_design(env)
    native = native_netlab_routers(env)
    found = {}
    for name, router in design.routers.items():
        for retained in router.retained:
            found.setdefault(retained.capability, retained.reason)
        if name not in native and router.bgp_multipath and router.neighbors:
            found.setdefault(
                "FRR eBGP maximum-paths",
                "netlab's FRR templates set no BGP maximum-paths",
            )
        validation = native.get(name)
        if validation and validation.netlab_retained_capability and router.neighbors:
            capability, reason = validation.netlab_retained_capability
            found.setdefault(capability, reason)
    return [{"capability": c, "reason": r} for c, r in found.items()]
