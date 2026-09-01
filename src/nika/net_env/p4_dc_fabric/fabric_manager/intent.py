"""Compile Clos topology into P4Runtime forwarding intent."""

from __future__ import annotations

from typing import Any

from nika.net_env.p4_dc_fabric.topology_model import (
    IPV4_LPM_SIZE,
    P4RUNTIME_PORT,
    PIPELINE_NAME,
    VIRTUAL_ROUTER_MAC,
    ClosFabricModel,
    gateway_ip,
    rack_prefix,
)


def _member(
    member_id: int,
    src_mac: str,
    dst_mac: str,
    port: int,
    *,
    peer: str,
    role: str,
) -> dict[str, Any]:
    return {
        "member_id": member_id,
        "src_mac": src_mac,
        "dst_mac": dst_mac,
        "port": port,
        "peer": peer,
        "role": role,
    }


def build_forwarding_intent(model: ClosFabricModel) -> dict[str, Any]:
    """Build per-switch LPM, ActionProfile members, and ECMP groups."""
    switches: dict[str, Any] = {}

    for leaf_id, leaf in enumerate(model.leaves, start=1):
        next_member = 1
        info = model.switch_info[leaf]
        members: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []
        ipv4_lpm: list[dict[str, Any]] = []

        for ep in model.endpoints_on_leaf(leaf_id):
            hport = model.port_to_peer(leaf, ep.name)
            assert hport is not None
            member_id = next_member
            next_member += 1
            members.append(
                _member(
                    member_id,
                    VIRTUAL_ROUTER_MAC,
                    ep.mac,
                    hport.bmv2_port,
                    peer=ep.name,
                    role="host",
                )
            )
            group_id = member_id
            groups.append(
                {"group_id": group_id, "member_ids": [member_id], "kind": "local"}
            )
            ipv4_lpm.append(
                {
                    "prefix": f"{ep.ip}/32",
                    "group_id": group_id,
                    "kind": "local_host",
                }
            )

        for dst_leaf_id in range(1, model.leaf_count + 1):
            if dst_leaf_id == leaf_id:
                continue
            member_ids: list[int] = []
            for spine in model.spines:
                sport = model.port_to_peer(leaf, spine)
                spine_port = model.port_to_peer(spine, leaf)
                assert sport is not None and spine_port is not None
                member_id = next_member
                next_member += 1
                members.append(
                    _member(
                        member_id,
                        sport.mac,
                        spine_port.mac,
                        sport.bmv2_port,
                        peer=spine,
                        role="spine",
                    )
                )
                member_ids.append(member_id)
            group_id = 100 + dst_leaf_id
            groups.append(
                {
                    "group_id": group_id,
                    "member_ids": member_ids,
                    "kind": "ecmp",
                    "dst_leaf_id": dst_leaf_id,
                }
            )
            ipv4_lpm.append(
                {
                    "prefix": rack_prefix(dst_leaf_id),
                    "group_id": group_id,
                    "kind": "remote_rack",
                    "dst_leaf_id": dst_leaf_id,
                }
            )

        switches[leaf] = {
            "name": leaf,
            "role": "leaf",
            "device_id": info.device_id,
            "address": f"{info.oob_ip}:{P4RUNTIME_PORT}",
            "leaf_id": leaf_id,
            "gateway_ip": gateway_ip(leaf_id),
            "rack_prefix": rack_prefix(leaf_id),
            "members": members,
            "groups": groups,
            "ipv4_lpm": ipv4_lpm,
        }

    for spine_id, spine in enumerate(model.spines, start=1):
        next_member = 1
        info = model.switch_info[spine]
        members = []
        groups = []
        ipv4_lpm = []
        for leaf_id, leaf in enumerate(model.leaves, start=1):
            lport = model.port_to_peer(spine, leaf)
            leaf_port = model.port_to_peer(leaf, spine)
            assert lport is not None and leaf_port is not None
            member_id = next_member
            next_member += 1
            members.append(
                _member(
                    member_id,
                    lport.mac,
                    leaf_port.mac,
                    lport.bmv2_port,
                    peer=leaf,
                    role="leaf",
                )
            )
            group_id = leaf_id
            groups.append(
                {
                    "group_id": group_id,
                    "member_ids": [member_id],
                    "kind": "to_leaf",
                    "dst_leaf_id": leaf_id,
                }
            )
            ipv4_lpm.append(
                {
                    "prefix": rack_prefix(leaf_id),
                    "group_id": group_id,
                    "kind": "rack",
                    "dst_leaf_id": leaf_id,
                }
            )
        switches[spine] = {
            "name": spine,
            "role": "spine",
            "device_id": info.device_id,
            "address": f"{info.oob_ip}:{P4RUNTIME_PORT}",
            "spine_id": spine_id,
            "members": members,
            "groups": groups,
            "ipv4_lpm": ipv4_lpm,
        }

    endpoints = [
        {
            "name": ep.name,
            "leaf_id": ep.leaf_id,
            "host_index": ep.host_index,
            "ip": ep.ip,
            "mac": ep.mac,
            "role": ep.role,
            "gateway_ip": gateway_ip(ep.leaf_id),
            "gateway_mac": VIRTUAL_ROUTER_MAC,
        }
        for ep in model.endpoints
    ]
    return {
        "topo_size": model.topo_size,
        "pipeline": {
            "name": PIPELINE_NAME,
            "ipv4_lpm_size": IPV4_LPM_SIZE,
            "ecmp_fanout": model.ecmp_fanout,
        },
        "spines": list(model.spines),
        "leaves": list(model.leaves),
        "switches": switches,
        "endpoints": endpoints,
        "web_urls": list(model.web_urls),
    }


def remote_rack_prefix(intent: dict[str, Any], switch: str) -> str | None:
    """Return a remote (or any) IPv4 LPM prefix programmed on ``switch``."""
    entries = intent.get("switches", {}).get(switch, {}).get("ipv4_lpm") or []
    for entry in entries:
        if entry.get("kind") in {"remote_rack", "rack"}:
            return str(entry["prefix"])
    if entries:
        return str(entries[0]["prefix"])
    return None
