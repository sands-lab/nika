"""Compile fixed ISP inputs through netlab's topology transformation and templates.

NetComplete chooses parameters; netlab renders device syntax. Neither tool
supplies the behavior predicates used to accept the resulting configuration.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from ipaddress import IPv4Network
from pathlib import Path

import yaml

from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.bgp.policy import BUSINESS_PREFIXES
from nika.net_env.isp.igp.plan import IspPlan
from nika.net_env.isp.igp.frr import isis_net_from_router_id

NETLAB_VERSION = "26.10"


def netlab_topology(plan: IspPlan, bgp: BgpPlan | None, device_profile: str) -> dict:
    """Translate explicit addresses, directed costs, ASes and policies to netlab.

    Scope is capability based: IPv4 OSPF with ordinary BGP, or FRR L2 IS-IS.
    Security overlays and unsupported devices fail before invoking the tool.
    """
    if device_profile not in {"frr", "iosxr"}:
        raise ValueError(
            "netlab construction currently supports FRR and IOS XR; RouterOS7 "
            "does not support upstream generic routing policies, and SR Linux "
            "requires a native CLI deployment adapter"
        )
    if plan.igp not in {"ospf", "isis"} or (
        plan.igp == "isis" and device_profile != "frr"
    ):
        raise ValueError("netlab ISP construction requires OSPF or FRR L2 IS-IS")
    if plan.igp == "isis" and bgp:
        raise ValueError(
            "netlab IS-IS/BGP composition is not covered by the concrete validation model"
        )
    if bgp and (
        bgp.mode not in {"ibgp_rr", "ebgp"}
        or any(bgp.inventory.get(k) for k in ("rpki", "rtbh", "leak_prefixes"))
        or any(
            n.rpki_cache
            or n.rov_reject_invalid
            or n.export_deny_prefixes
            or n.rtbh_import_policy
            or n.ebgp_outbound_route_maps
            for n in bgp.nodes
        )
    ):
        raise ValueError("netlab ISP construction does not support security overlays")
    from nika.net_env.isp.iosxr import iosxr_interface

    native = iosxr_interface if device_profile == "iosxr" else lambda name: name
    bgp_nodes = {n.device_name: n for n in bgp.nodes} if bgp else {}
    if bgp and set(bgp_nodes) != {n.device_name for n in plan.nodes}:
        raise ValueError("BGP and IGP plans must contain the same nodes")
    topology = {
        "name": "nika",
        "provider": "clab",  # Transformation only: NIKA still owns deployment.
        "defaults.device": device_profile,
        "defaults.sources.list": ["package:topology-defaults.yml"],
        "defaults.const.MAX_NODE_ID": max(150, len(plan.nodes) + 1),
        "defaults.const.MAX_NODE_ID_LENGTH": 63,
        "addressing.mgmt.ipv4": "172.31.0.0/16",
        "module": [plan.igp],
        plan.igp: {"process": 1, "area": "0.0.0.0"}
        if plan.igp == "ospf"
        else {"instance": "NIKA", "type": "level-2", "af": {"ipv4": True}},
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
    if bgp:
        topology.update(
            {
                "module": [plan.igp, "bgp", "routing"],
                "plugin": ["bgp.policy"],
                "bgp": {
                    "advertise_loopback": False,
                    "advertise_roles": [],
                    "next_hop_self": False,  # Preserve reflected next hops (RFC 4456).
                },
            }
        )
    for node in plan.nodes:
        data = {
            "loopback": {
                "ipv4": f"{node.loopback}/32",
            },
        }
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
            business = sorted(
                {
                    *BUSINESS_PREFIXES,
                    *(o.prefix for o in bgp.originated),
                }
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
            if peer and peer.session_type == "ebgp":
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
                                else "lo1",
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
    try:
        installed = version("networklab")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "Install the construction tool with uv sync --extra netlab"
        ) from exc
    if installed != NETLAB_VERSION:
        raise RuntimeError(f"Expected networklab=={NETLAB_VERSION}, found {installed}")
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "topology.yml").write_text(yaml.safe_dump(topology, sort_keys=False))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from netsim.cli import main; main()",
            "create",
            "topology.yml",
            "-o",
            "config",
            "-o",
            "json=transformed.json",
        ],
        cwd=directory,
        env={k: v for k, v in os.environ.items() if not k.startswith("NETLAB_")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    (directory / "netlab.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"netlab compilation failed; see {directory / 'netlab.log'}")
    transformed = json.loads((directory / "transformed.json").read_text())
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
    configs = {}
    for node in plan.nodes:
        root = directory / "node_files" / node.device_name
        modules = ["initial", *transformed["nodes"][node.device_name]["module"]]
        modules += transformed["nodes"][node.device_name].get("config", [])
        fragments = []
        for module in modules:
            fragment = (root / module).read_text()
            if device_profile == "frr" and module == "initial":
                # Upstream packages FRR initial CLI in a shell heredoc. Extract
                # the literal CLI; NIKA already owns Linux interface creation.
                marker = "cat >/tmp/config <<CONFIG\n"
                if fragment.count(marker) != 1:
                    raise ValueError("Unsupported netlab FRR initial config envelope")
                fragment = fragment.split(marker)[1].split("\nCONFIG\n", 1)[0]
            # 'do write' is an interactive persistence command, not startup CLI.
            fragments.append(
                "\n".join(
                    line for line in fragment.splitlines() if line.strip() != "do write"
                )
            )
        configs[node.device_name] = "\n!\n".join(fragments) + "\n"
    return configs
