"""Deterministic inject params for the ISP scenario from compiled inventory."""

from __future__ import annotations

from typing import Any, Mapping

# Synthetic hijack prefix outside ISP business pools (203.0.113/24, 198.51.100/24).
DEFAULT_HIJACK_PREFIX = "198.18.0.0/24"

LINK_INTF_PROBLEMS = frozenset(
    {
        "link_down",
        "link_flap",
        "link_detach",
    }
)
LINK_HOST_ONLY_PROBLEMS = frozenset(
    {
        "mtu_mismatch",
        "link_bandwidth_throttling",
        "link_high_packet_corruption",
        "icmp_acl_block",
        "frr_service_down",
        "ospf_area_misconfiguration",
        "ospf_neighbor_missing",
        "ospf_acl_block",
    }
)
BGP_ORIGINATOR_PROBLEMS = frozenset(
    {
        "bgp_asn_misconfig",
        "bgp_acl_block",
        "bgp_missing_route_advertisement",
        "bgp_blackhole_route_leak",
        "host_static_blackhole",
    }
)
BGP_HIJACK_PROBLEM = "bgp_hijacking"


def first_link_endpoint(isp_inventory: Mapping[str, Any]) -> tuple[str, str]:
    """Return (device, iface) for the first inventory link endpoint_a."""
    links = list(isp_inventory.get("links") or [])
    if not links:
        raise ValueError("ISP inventory has no links")
    link = sorted(links, key=lambda item: str(item.get("link_id") or ""))[0]
    ep = link.get("endpoint_a") or {}
    device = ep.get("device")
    iface = ep.get("iface")
    if not device or not iface:
        raise ValueError(
            f"link {link.get('link_id')!r} missing endpoint_a device/iface"
        )
    return str(device), str(iface)


def first_router(isp_inventory: Mapping[str, Any]) -> str:
    nodes = list(isp_inventory.get("nodes") or [])
    if not nodes:
        raise ValueError("ISP inventory has no nodes")
    devices = sorted(str(n["device"]) for n in nodes if n.get("device"))
    if not devices:
        raise ValueError("ISP inventory nodes lack device names")
    return devices[0]


def first_originator(bgp_inventory: Mapping[str, Any] | None) -> str:
    if not bgp_inventory:
        raise ValueError("bgp inventory required for BGP inject targets")
    originated = list(bgp_inventory.get("originated") or [])
    if originated:
        rows = sorted(
            originated,
            key=lambda item: (
                str(item.get("device") or ""),
                str(item.get("prefix") or ""),
            ),
        )
        device = rows[0].get("device")
        if device:
            return str(device)
    nodes = list(bgp_inventory.get("nodes") or [])
    devices = sorted(str(n["device"]) for n in nodes if n.get("device"))
    if not devices:
        raise ValueError("bgp inventory has no devices")
    return devices[0]


def hijack_speaker_and_prefix(
    bgp_inventory: Mapping[str, Any] | None,
    *,
    hijack_prefix: str = DEFAULT_HIJACK_PREFIX,
) -> tuple[str, str]:
    """Prefer a non-originator BGP speaker; fall back to first BGP node."""
    if not bgp_inventory:
        raise ValueError("bgp inventory required for bgp_hijacking")
    originators = {
        str(item["device"])
        for item in (bgp_inventory.get("originated") or [])
        if item.get("device")
    }
    nodes = sorted(
        str(n["device"]) for n in (bgp_inventory.get("nodes") or []) if n.get("device")
    )
    if not nodes:
        raise ValueError("bgp inventory has no devices")
    non_orig = [d for d in nodes if d not in originators]
    speaker = non_orig[0] if non_orig else nodes[0]
    return speaker, hijack_prefix


def isp_inject_params(
    problem: str,
    isp_inventory: Mapping[str, Any],
    bgp_inventory: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build ``--set``-style inject overrides for an ISP-compatible problem."""
    if problem in LINK_INTF_PROBLEMS:
        device, iface = first_link_endpoint(isp_inventory)
        return {"host_name": device, "intf_name": iface}
    if problem in LINK_HOST_ONLY_PROBLEMS:
        return {"host_name": first_router(isp_inventory)}
    if problem in BGP_ORIGINATOR_PROBLEMS:
        return {"host_name": first_originator(bgp_inventory)}
    if problem == BGP_HIJACK_PROBLEM:
        host, prefix = hijack_speaker_and_prefix(bgp_inventory)
        return {"host_name": host, "target_network": prefix}
    raise ValueError(f"unsupported isp inject problem: {problem!r}")
