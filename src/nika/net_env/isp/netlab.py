"""Compile fixed ISP inputs through netlab's topology transformation and templates.

NetComplete chooses parameters; netlab renders device syntax. Neither tool
supplies the behavior predicates used to accept the resulting configuration.
"""

from __future__ import annotations

from ipaddress import IPv4Network
import re

import yaml
from pathlib import Path

from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.bgp.policy import business_prefixes
from nika.net_env.isp.igp.plan import IspPlan
from nika.net_env.isp.igp.frr import ISIS_PROCESS, isis_net_from_router_id
from nika.net_env.isp.igp.ifaces import srl_ethernet_name
from nika.net_env.isp.containerlab.lab import SRL_IMAGE, SRL_TYPE
from nika.net_env.synthesis.netlab import run_netlab


def netlab_topology(plan: IspPlan, bgp: BgpPlan | None, device_profile: str) -> dict:
    """Translate explicit addresses, directed costs, ASes and policies to netlab.

    Scope is capability based: IPv4 OSPF or L2 IS-IS with ordinary BGP,
    plus the existing FRR OSPF/eBGP security overlays.
    """
    if device_profile not in {"frr", "iosxr", "nokia_srlinux", "routeros"}:
        raise ValueError(
            "netlab construction currently supports FRR, IOS XR, Nokia SR Linux and RouterOS"
        )
    if plan.igp not in {"ospf", "isis"} or (
        plan.igp == "isis" and device_profile not in {"frr", "iosxr", "nokia_srlinux"}
    ):
        raise ValueError("netlab ISP construction requires OSPF or supported L2 IS-IS")
    if bgp and bgp.mode not in {"ibgp_rr", "ebgp"}:
        raise ValueError("netlab ISP construction requires a supported BGP mode")
    security = bgp and (
        any(bgp.inventory.get(k) for k in ("rpki", "rtbh", "leak_prefixes"))
        or any(
            n.rpki_cache
            or n.rov_reject_invalid
            or n.export_deny_prefixes
            or n.rtbh_import_policy
            or n.ebgp_outbound_route_maps
            for n in bgp.nodes
        )
    )
    if security and (
        device_profile != "frr"
        or plan.igp != "ospf"
        or bgp.mode != "ebgp"
        or bool(bgp.inventory.get("rpki")) == bool(bgp.inventory.get("rtbh"))
    ):
        raise ValueError(
            "netlab security overlays require FRR OSPF/eBGP and one security profile"
        )
    from nika.net_env.isp.iosxr import iosxr_interface
    from nika.net_env.isp.routeros import routeros_interface

    native = (
        iosxr_interface
        if device_profile == "iosxr"
        else srl_ethernet_name
        if device_profile == "nokia_srlinux"
        else routeros_interface
        if device_profile == "routeros"
        else lambda name: name
    )
    netlab_device = {
        "nokia_srlinux": "srlinux",
        "routeros": "routeros7",
    }.get(device_profile, device_profile)
    bgp_nodes = {n.device_name: n for n in bgp.nodes} if bgp else {}
    if bgp and set(bgp_nodes) != {n.device_name for n in plan.nodes}:
        raise ValueError("BGP and IGP plans must contain the same nodes")
    topology = {
        "name": "nika",
        "provider": "clab",  # Transformation only: NIKA still owns deployment.
        "defaults.device": netlab_device,
        "defaults.sources.list": ["package:topology-defaults.yml"],
        "defaults.const.MAX_NODE_ID": max(150, len(plan.nodes) + 1),
        "defaults.const.MAX_NODE_ID_LENGTH": 63,
        "addressing.mgmt.ipv4": "172.31.0.0/16",
        "module": [plan.igp],
        plan.igp: {"process": 1, "area": "0.0.0.0"}
        if plan.igp == "ospf"
        else {
            "instance": "default"
            if device_profile == "nokia_srlinux"
            else ISIS_PROCESS,
            "type": "level-2",
            "af": {"ipv4": True},
        },
        "nodes": {},
        "links": [],
    }
    if device_profile == "frr" and plan.igp == "isis":
        # NIKA owns Linux interface/management setup; preserve FRR defaults used
        # by its existing runtime and the concrete IS-IS validation subset.
        topology["defaults.devices.frr.clab.group_vars.netlab_mgmt_vrf"] = False
        topology["defaults.devices.frr.clab.group_vars.netlab_frr_defaults"] = (
            "traditional"
        )
        topology["defaults.devices.frr.features.isis.loopback.passive"] = True
    if device_profile == "iosxr":
        # The ISP design announces router loopbacks as passive IGP interfaces,
        # as NIKA's native XR renderer does; upstream XR templates do not.
        topology[f"defaults.devices.iosxr.features.{plan.igp}.loopback.passive"] = True
    if bgp:
        topology.update(
            {
                "module": [plan.igp, "bgp"]
                if device_profile == "routeros"
                else [plan.igp, "bgp", "routing"],
                "bgp": {
                    "advertise_loopback": False,
                    "advertise_roles": [],
                    # Preserve reflected next hops (RFC 4456). NIKA's FRR eBGP
                    # design sets next-hop-self on intra-AS sessions instead.
                    "next_hop_self": device_profile == "frr" and bgp.mode == "ebgp",
                },
            }
        )
        if device_profile != "routeros":
            topology["plugin"] = ["bgp.policy"]
    for node in plan.nodes:
        data = {
            "loopback": {
                "ipv4": f"{node.loopback}/32",
            },
        }
        if device_profile == "nokia_srlinux":
            data.update({"box": SRL_IMAGE, "clab.type": SRL_TYPE})
        elif device_profile == "routeros":
            from nika.net_env.routeros_simple_bgp.lab import IMAGE

            data.update({"box": IMAGE, "clab.type": "vr-ros"})
        if plan.igp == "ospf":
            data["ospf.router_id"] = node.router_id
        else:
            data["isis.net"] = isis_net_from_router_id(node.router_id)
        if bgp:
            bnode = bgp_nodes[node.device_name]
            data.update(
                {
                    "bgp.as": bnode.asn,
                    "bgp.router_id": bnode.router_id,
                }
            )
            if "rr" in bnode.roles:
                data["bgp.rr"] = True
            if bnode.cluster_id:
                data["bgp.rr_cluster_id"] = bnode.cluster_id
            business = business_prefixes(o.prefix for o in bgp.originated)
            if device_profile == "nokia_srlinux":
                # Keep the native SR Linux allow-list.
                from nika.net_env.isp.bgp.srl import SRL_BUSINESS_PREFIXES

                business = sorted(
                    {*SRL_BUSINESS_PREFIXES, *(o.prefix for o in bgp.originated)}
                )
            prefixes = {"BUSINESS": [{"ipv4": p} for p in business]}
            policies = {
                name: [{"action": "permit", "match": {"prefix": "BUSINESS"}}]
                for name in ("BGP-IN", "BGP-OUT")
            }
            imports = dict(bnode.import_policies)
            for i, session in enumerate(bnode.sessions):
                if session.remote_ip not in imports:
                    continue
                if session.session_type != "ebgp":
                    raise ValueError("netlab import policies require an external peer")
                entries = []
                for j, term in enumerate(imports[session.remote_ip]):
                    name = f"NC_PFX_{i}_{j}"
                    prefixes[name] = [{"ipv4": term.prefix}]
                    entry = {
                        "action": "permit"
                        if term.permit and term.prefix in business
                        else "deny",
                        "match": {"prefix": name},
                    }
                    if (
                        entry["action"] == "permit"
                        and term.local_preference is not None
                    ):
                        entry["set"] = {"locpref": term.local_preference}
                    entries.append(entry)
                policies[f"NC_IN_{i}"] = entries + policies["BGP-IN"]
            if device_profile != "routeros":
                data["routing"] = {"prefix": prefixes, "policy": policies}
        topology["nodes"][node.device_name] = data

    by_interface = {
        (n.device_name, i.name): i for n in plan.nodes for i in n.interfaces
    }

    def endpoint(device, interface):
        iface = by_interface[device, interface]
        maximum = 65534 if plan.igp == "ospf" else 16777214
        if not 1 <= iface.metric <= maximum:
            raise ValueError(f"netlab {plan.igp} costs must be in 1..{maximum}")
        result = {
            "node": device,
            "ifname": native(interface),
            "ipv4": f"{iface.address}/{iface.prefixlen}",
            plan.igp: {"cost": iface.metric, "passive": iface.passive},
        }
        if plan.igp == "isis":
            result["isis"]["type"] = "level-2"
        if bgp:
            result["bgp.advertise"] = False
            bnode = bgp_nodes[device]
            peer = next(
                (s for s in bnode.sessions if s.remote_ip == iface.peer_address), None
            )
            if peer and peer.session_type == "ebgp" and device_profile != "routeros":
                index = bnode.sessions.index(peer)
                policy = (
                    f"NC_IN_{index}"
                    if peer.remote_ip in dict(bnode.import_policies)
                    else "BGP-IN"
                )
                result["bgp.policy"] = {"in": policy, "out": "BGP-OUT"}
        return result

    backbone = set()
    for link in plan.links:
        backbone.update(
            ((link.endpoint_a, link.iface_a), (link.endpoint_b, link.iface_b))
        )
        topology["links"].append(
            {
                "type": "p2p",
                # Keep external subnet reachability inside each AS, without peers.
                "role": "passive"
                if by_interface[link.endpoint_a, link.iface_a].passive
                else "internal",
                "interfaces": [
                    endpoint(link.endpoint_a, link.iface_a),
                    endpoint(link.endpoint_b, link.iface_b),
                ],
            }
        )
    for node in plan.nodes:
        for iface in node.interfaces:
            if (node.device_name, iface.name) not in backbone:
                topology["links"].append(
                    {
                        "type": "stub",
                        "interfaces": [endpoint(node.device_name, iface.name)],
                    }
                )
        if bgp:
            for index, origin in enumerate(bgp_nodes[node.device_name].originated, 1):
                topology["links"].append(
                    {
                        "type": "loopback",
                        plan.igp: False,
                        "bgp.advertise": True,
                        "interfaces": [
                            {
                                "node": node.device_name,
                                "ifname": f"Loopback{index}"
                                if device_profile == "iosxr"
                                else f"lo0.{index}"
                                if device_profile == "nokia_srlinux"
                                else "lo1"
                                if device_profile == "routeros"
                                else f"lo{index}",
                                "ipv4": f"{origin.ping_address}/{IPv4Network(origin.prefix).prefixlen}",
                            }
                        ],
                    }
                )
    return topology


def generate_netlab_configs(
    plan: IspPlan, bgp: BgpPlan | None, device_profile: str, directory: Path
) -> dict[str, str]:
    """Run pinned upstream compilation offline and retain input/model/fragments.

    The caller owns the directory: netlab replaces its node_files subdirectory.
    No provider deployment or shell configuration fragment is executed.
    """
    topology = netlab_topology(plan, bgp, device_profile)
    configs, transformed = run_netlab(topology, directory)
    if bgp and device_profile != "routeros":
        configs = _bind_ibgp_policies(configs, transformed, bgp.mode)
    if device_profile == "frr" and bgp:
        # netlab requires unique virtual interface identities. NIKA already owns
        # one dummy interface with multiple origin addresses; bind those emitted
        # address blocks back to that same fixed Linux interface.
        for node in bgp.nodes:
            for index in range(2, len(node.originated) + 1):
                configs[node.device_name] = re.sub(
                    rf"^interface lo{index}$",
                    "interface lo1",
                    configs[node.device_name],
                    flags=re.M,
                )
        from nika.net_env.isp.netlab_security import apply_netlab_security_overlay

        configs = apply_netlab_security_overlay(configs, transformed, bgp)
        # Existing incident injectors address these fixed admission policies.
        # Bind the upstream AF-qualified names consistently at definitions and
        # references; the generated prefix tests and actions remain unchanged.
        aliases = {
            "BUSINESS-ipv4": "BUSINESS",
            "BGP-IN-ipv4": "BGP-IN",
            "BGP-OUT-ipv4": "BGP-OUT",
        }
        for name, text in configs.items():
            configs[name] = re.sub(
                r"(?<!\S)(BUSINESS-ipv4|BGP-IN-ipv4|BGP-OUT-ipv4)(?!\S)",
                lambda match: aliases[match[1]],
                text,
            )
    if device_profile == "routeros":
        configs = _routeros_policy_fragments(configs, transformed, plan, bgp)
    # Netlab derives sessions from explicit AS/RR inputs. Reject a different sketch
    # rather than silently changing the authored control-plane design.
    actual, expected = set(), set()
    if bgp:
        expected = {
            (s.local_device, s.remote_ip, s.remote_asn, s.route_reflector_client)
            for s in bgp.sessions
        }
        actual = {
            (name, s["ipv4"], s["as"], bool(s.get("rr_client")))
            for name, n in transformed["nodes"].items()
            for s in n["bgp"]["neighbors"]
        }
    if actual != expected:
        raise ValueError("netlab-derived BGP sessions differ from the fixed sketch")
    if bgp:
        origins = {(o.device, o.prefix) for o in bgp.originated}
        advertised = {
            (name, prefix["ipv4"])
            for name, node in transformed["nodes"].items()
            for prefix in node["bgp"].get("advertise", [])
        }
        if advertised != origins:
            raise ValueError(
                "netlab-derived prefix origins differ from the fixed sketch"
            )
    return configs


def _routeros_policy_fragments(configs, transformed, plan, bgp):
    """Complete RouterOS areas netlab 26.10 does not generate (origins/policy)."""
    if bgp is None:
        return configs
    from nika.net_env.isp.routeros import _filter_rule

    bgp_nodes = {node.device_name: node for node in bgp.nodes}
    result = dict(configs)
    business = business_prefixes(o.prefix for o in bgp.originated)
    for name, node in transformed["nodes"].items():
        bnode = bgp_nodes[name]
        lines = ["# Policy bindings and business origins"]
        origins = sorted({o.prefix for o in bnode.originated})
        current = configs[name]
        if origins and "name=lo1 protocol-mode=none" not in current:
            lines.append("/interface/bridge add name=lo1 protocol-mode=none")
            for origin in sorted(bnode.originated, key=lambda item: item.prefix):
                plen = IPv4Network(origin.prefix).prefixlen
                if f"address={origin.ping_address}/{plen}" not in current:
                    lines.append(
                        f"/ip/address add interface=lo1 address={origin.ping_address}/{plen}"
                    )
        for origin in sorted(bnode.originated, key=lambda item: item.prefix):
            if f"list=bgp-networks address={origin.prefix}" not in current:
                lines.append(
                    f"/ip/firewall/address-list add list=bgp-networks address={origin.prefix}"
                )
        for prefix in business:
            lines.append(
                _filter_rule("POLICY-BGP-IN", f"if (dst == {prefix}) {{ accept; }}")
            )
            lines.append(
                _filter_rule("POLICY-BGP-OUT", f"if (dst == {prefix}) {{ accept; }}")
            )
        for chain in ("POLICY-BGP-IN", "POLICY-BGP-OUT"):
            lines.append(_filter_rule(chain, "reject;"))
        imports = dict(bnode.import_policies)
        for index, session in enumerate(
            sorted(bnode.sessions, key=lambda s: (s.remote_ip, s.remote_device))
        ):
            chain = f"POLICY-NC-IN-{index}"
            if session.remote_ip in imports:
                for term in imports[session.remote_ip]:
                    action = "jump POLICY-BGP-IN;" if term.permit else "reject;"
                    if term.permit and term.local_preference is not None:
                        action = f"set bgp-local-pref {term.local_preference}; {action}"
                    lines.append(
                        _filter_rule(chain, f"if (dst == {term.prefix}) {{ {action} }}")
                    )
                lines.append(_filter_rule(chain, "jump POLICY-BGP-IN;"))
            else:
                chain = "POLICY-BGP-IN"
            peer = session.remote_ip.replace(".", "_")
            lines.append(
                f"/routing/bgp/connection set [/routing/bgp/connection find name={peer}] "
                f"input.filter={chain} output.filter-chain=POLICY-BGP-OUT local.address={session.local_ip}"
            )
        result[name] += "\n" + "\n".join(lines) + "\n"
    return result


def _bind_ibgp_policies(configs, transformed, mode):
    """Attach upstream-generated admission policies to actual iBGP neighbors.

    netlab 26.10 bgp.policy binds external peers only. NIKA's fixed sketch applies
    the same admission policy within an AS. Keep that binding while letting the
    upstream routing module generate the prefix tests and policy actions.
    NIKA's FRR eBGP design imports with BGP-IN on iBGP but exports without
    BGP-OUT, so border routers receive leak prefixes inside their AS.
    """
    result = dict(configs)
    for name, node in transformed["nodes"].items():
        peers = [
            p["ipv4"]
            for p in node.get("bgp", {}).get("neighbors", [])
            if p["type"] == "ibgp"
        ]
        if not peers:
            continue
        if node["device"] == "srlinux":
            document = yaml.safe_load(result[name])
            for peer in peers:
                base = f"/network-instance[name=default]/protocols/bgp/neighbor[peer-address={peer}]/afi-safi[afi-safi-name=ipv4-unicast]"
                for direction, policies in (
                    ("import", ["BGP-IN"]),
                    ("export", ["default_bgp_export", "BGP-OUT"]),
                ):
                    document["operations"].append(
                        {
                            "operation": "replace",
                            "path": base + f"/{direction}-policy",
                            "value": policies,
                        }
                    )
            result[name] = yaml.safe_dump(document, sort_keys=False)
            continue
        asn = node["bgp"]["as"]
        lines = [f"router bgp {asn}"]
        if node["device"] == "frr":
            bindings = (("BGP-IN-ipv4", "in"),)
            if mode != "ebgp":
                bindings += (("BGP-OUT-ipv4", "out"),)
            for policy, _ in bindings:
                if not re.search(rf"^route-map {policy} ", result[name], re.M):
                    raise ValueError("Missing upstream iBGP admission policy")
            lines.append(" address-family ipv4 unicast")
            for peer in peers:
                lines.extend(
                    f"  neighbor {peer} route-map {policy} {direction}"
                    for policy, direction in bindings
                )
            lines.append(" exit-address-family")
        elif node["device"] == "iosxr":
            for policy in ("BGP-IN", "BGP-OUT"):
                if not re.search(rf"^route-policy {policy}$", result[name], re.M):
                    raise ValueError("Missing upstream iBGP admission policy")
            for peer in peers:
                lines.extend(
                    [
                        f" neighbor {peer}",
                        "  address-family ipv4 unicast",
                        "   route-policy BGP-IN in",
                        "   route-policy BGP-OUT out",
                        "  !",
                        " !",
                    ]
                )
        else:
            raise ValueError("Unsupported iBGP policy-binding device")
        result[name] += "\n" + "\n".join(lines) + "\n!\n"
    return result
