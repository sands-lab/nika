"""OpenFlow flow and SELECT-group rules for the L3 Clos fabric."""

from __future__ import annotations

from typing import Any

from nika.net_env.sdn_l3_clos.topology_model import (
    VIRTUAL_ROUTER_MAC,
    ClosFabricModel,
    device_id,
    dpid_for_leaf,
    dpid_for_spine,
    gateway_ip,
    rack_prefix,
)


def _group_id_for_prefix(leaf_id: int) -> int:
    return 0x1000 + leaf_id


def build_forwarding_rules(model: ClosFabricModel) -> dict[str, Any]:
    """Build per-switch OpenFlow flows and ECMP groups from the Clos model."""
    groups: list[dict[str, Any]] = []
    flows: list[dict[str, Any]] = []

    for leaf_id, leaf in enumerate(model.leaves, start=1):
        leaf_ports = model.ports[leaf]
        host_ports = [p for p in leaf_ports if p.role == "host"]
        spine_ports = [p for p in leaf_ports if p.role == "spine"]

        # Local delivery: dest host IP → host port + rewrite
        for ep, hport in zip(model.endpoints_on_leaf(leaf_id), host_ports, strict=True):
            flows.append(
                {
                    "switch": leaf,
                    "device_id": device_id(dpid_for_leaf(leaf_id)),
                    "table": 0,
                    "priority": 40000,
                    "match": f"ip,nw_dst={ep.ip}",
                    "actions": (
                        f"dec_ttl,"
                        f"mod_dl_src:{VIRTUAL_ROUTER_MAC},"
                        f"mod_dl_dst:{ep.mac},"
                        f"output:{hport.name}"
                    ),
                    "cookie": f"0x2{leaf_id:03x}{ep.host_index:02x}",
                }
            )

        # Remote rack prefixes → SELECT ECMP over spines
        for dst_leaf_id in range(1, model.leaf_count + 1):
            if dst_leaf_id == leaf_id:
                continue
            gid = _group_id_for_prefix(dst_leaf_id)
            buckets = []
            for sport in spine_ports:
                spine = sport.peer
                spine_id = model.spine_id(spine)
                # Facing MAC on spine for this leaf
                spine_port = model.port_to_peer(spine, leaf)
                assert spine_port is not None
                buckets.append(
                    {
                        "actions": (
                            f"mod_dl_src:{sport.mac},"
                            f"mod_dl_dst:{spine_port.mac},"
                            f"output:{sport.name}"
                        ),
                        "spine": spine,
                        "spine_id": spine_id,
                    }
                )
            groups.append(
                {
                    "switch": leaf,
                    "device_id": device_id(dpid_for_leaf(leaf_id)),
                    "group_id": gid,
                    "type": "select",
                    "selection_method": "hash",
                    "fields": ["nw_src", "nw_dst", "tp_src", "tp_dst", "nw_proto"],
                    "buckets": buckets,
                    "prefix": rack_prefix(dst_leaf_id),
                }
            )
            prefix = rack_prefix(dst_leaf_id)
            flows.append(
                {
                    "switch": leaf,
                    "device_id": device_id(dpid_for_leaf(leaf_id)),
                    "table": 0,
                    "priority": 30000,
                    "match": f"ip,nw_dst={prefix}",
                    "actions": f"dec_ttl,group:{gid}",
                    "cookie": f"0x3{leaf_id:03x}{dst_leaf_id:03x}",
                }
            )

        # Drop non-IP that is not LLDP/ARP handling: allow ARP locally for hosts
        # Endpoints use static neigh for GW; still allow ARP among hosts on rack.
        for hport in host_ports:
            flows.append(
                {
                    "switch": leaf,
                    "device_id": device_id(dpid_for_leaf(leaf_id)),
                    "table": 0,
                    "priority": 42000,
                    "match": f"arp,in_port={hport.name}",
                    "actions": f"output:{hport.name}",
                    "cookie": "0x1001",
                    "note": "placeholder-overwritten-below",
                }
            )

    # Fix ARP: flood ARP within the rack host ports only (simple L2 for ARP)
    flows = [f for f in flows if f.get("note") != "placeholder-overwritten-below"]
    for leaf_id, leaf in enumerate(model.leaves, start=1):
        host_ports = [p for p in model.ports[leaf] if p.role == "host"]
        if len(host_ports) == 1:
            # single host: ARP reply not needed across hosts; drop ARP to fabric
            flows.append(
                {
                    "switch": leaf,
                    "device_id": device_id(dpid_for_leaf(leaf_id)),
                    "table": 0,
                    "priority": 42000,
                    "match": "arp",
                    "actions": "drop",
                    "cookie": "0x1001",
                }
            )
        else:
            flood = ",".join(p.name for p in host_ports)
            for hport in host_ports:
                others = ",".join(p.name for p in host_ports if p.name != hport.name)
                flows.append(
                    {
                        "switch": leaf,
                        "device_id": device_id(dpid_for_leaf(leaf_id)),
                        "table": 0,
                        "priority": 42000,
                        "match": f"arp,in_port={hport.name}",
                        "actions": f"output:{others}" if others else "drop",
                        "cookie": "0x1001",
                    }
                )
            _ = flood

        # Gateway ARP: reply for virtual router (Nicira extensions)
        gw = gateway_ip(leaf_id)
        flows.append(
            {
                "switch": leaf,
                "device_id": device_id(dpid_for_leaf(leaf_id)),
                "table": 0,
                "priority": 45000,
                "match": f"arp,arp_tpa={gw},arp_op=1",
                "actions": (
                    f"move:NXM_OF_ETH_SRC[]->NXM_OF_ETH_DST[],"
                    f"mod_dl_src:{VIRTUAL_ROUTER_MAC},"
                    f"load:0x2->NXM_OF_ARP_OP[],"
                    f"move:NXM_NX_ARP_SHA[]->NXM_NX_ARP_THA[],"
                    f"move:NXM_OF_ARP_SPA[]->NXM_OF_ARP_TPA[],"
                    f"load:0x020000000001->NXM_NX_ARP_SHA[],"
                    f"load:{_ip_to_hex(gw)}->NXM_OF_ARP_SPA[],"
                    f"in_port"
                ),
                "cookie": "0x4500",
            }
        )

    # Spine: IPv4 toward destination leaf
    for spine_id, spine in enumerate(model.spines, start=1):
        for leaf_id, leaf in enumerate(model.leaves, start=1):
            lport = model.port_to_peer(spine, leaf)
            assert lport is not None
            leaf_face = model.port_to_peer(leaf, spine)
            assert leaf_face is not None
            prefix = rack_prefix(leaf_id)
            flows.append(
                {
                    "switch": spine,
                    "device_id": device_id(dpid_for_spine(spine_id)),
                    "table": 0,
                    "priority": 30000,
                    "match": f"ip,nw_dst={prefix}",
                    "actions": (
                        f"dec_ttl,"
                        f"mod_dl_src:{lport.mac},"
                        f"mod_dl_dst:{leaf_face.mac},"
                        f"output:{lport.name}"
                    ),
                    "cookie": f"0x4{spine_id:03x}{leaf_id:03x}",
                }
            )

    return {
        "virtual_router_mac": VIRTUAL_ROUTER_MAC,
        "ecmp_fanout": model.ecmp_fanout,
        "spine_count": model.spine_count,
        "leaf_count": model.leaf_count,
        "groups": groups,
        "flows": flows,
        "expected_devices": model.expected_device_ids(),
        "expected_leaf_spine_links": model.expected_leaf_spine_link_count(),
    }


def _ip_to_hex(ip: str) -> str:
    parts = [int(p) for p in ip.split(".")]
    value = (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3]
    return f"0x{value:08x}"
