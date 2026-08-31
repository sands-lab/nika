"""Deterministic inject params for the ISP scenario from compiled inventory."""

from __future__ import annotations

import ipaddress
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
LINK_BACKBONE_PROBLEMS = frozenset(
    {
        "link_capacity_bottleneck",
        "link_packet_corruption",
    }
)
LINK_HOST_ONLY_PROBLEMS = frozenset(
    {
        "link_capacity_bottleneck",
        "link_packet_corruption",
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
        "host_static_blackhole",
    }
)
BGP_HIJACK_PROBLEM = "bgp_hijacking"
BGP_RPKI_LEAK_PROBLEM = "bgp_rpki_invalid_route_leak"
BGP_RTBH_LEAK_PROBLEM = "bgp_blackhole_community_leak"
BGP_MAX_PREFIX_PROBLEM = "bgp_max_prefix_exceeded"


def link_peer_endpoint(
    isp_inventory: Mapping[str, Any], device: str, iface: str
) -> tuple[str, str]:
    """Return ``(peer_device, peer_iface)`` for one backbone link endpoint."""
    for link in isp_inventory.get("links") or []:
        for side, peer_side in (
            ("endpoint_a", "endpoint_b"),
            ("endpoint_b", "endpoint_a"),
        ):
            ep = link.get(side) or {}
            if ep.get("device") == device and ep.get("iface") == iface:
                peer = link.get(peer_side) or {}
                peer_device = peer.get("device")
                peer_iface = peer.get("iface")
                if peer_device and peer_iface:
                    return str(peer_device), str(peer_iface)
    raise ValueError(f"ISP inventory has no link for endpoint {device!r} {iface!r}")


def stub_host_for_router(router_device: str) -> str:
    from nika.net_env.isp.traffic.models import stub_host_name

    return stub_host_name(router_device)


def stub_host_ip(isp_inventory: Mapping[str, Any], router_device: str) -> str:
    want = stub_host_for_router(router_device)
    for row in isp_inventory.get("hosts") or []:
        host = str(row.get("host") or "")
        if host == want or row.get("router_device") == router_device:
            address = str(row.get("address") or "")
            if address:
                return address.split("/")[0]
    raise ValueError(f"ISP inventory has no stub host for router {router_device!r}")


def isp_link_symptom_targets(
    isp_inventory: Mapping[str, Any], device: str, iface: str
) -> dict[str, str]:
    """Probe endpoints that traverse ``device``/``iface`` toward the link peer."""
    peer_device, _ = link_peer_endpoint(isp_inventory, device, iface)
    src = stub_host_for_router(device)
    return {
        "symptom_host": src,
        "probe_dst_ip": stub_host_ip(isp_inventory, peer_device),
        "peer_host": stub_host_for_router(peer_device),
    }


def isp_default_probe_path(isp_inventory: Mapping[str, Any]):
    """Default inter-PoP probe path from the first backbone link."""
    from nika.problems.support.probe_paths import ProbePath

    device, iface = first_link_endpoint(isp_inventory)
    targets = isp_link_symptom_targets(isp_inventory, device, iface)
    return ProbePath(
        src_host=targets["symptom_host"],
        dst_ip=targets["probe_dst_ip"],
        control_plane_host=device,
        peer_host=targets["peer_host"],
    )


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


def first_ebgp_session(
    bgp_inventory: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Return one undirected eBGP session as receiver/peer/neighbor_ip.

    Prefers a session whose peer originates a business prefix so session reset
    withdraws observable RIB state. ``neighbor_ip`` is the peer address as seen
    from the receiver.
    """
    if not bgp_inventory:
        raise ValueError("bgp inventory required for bgp_max_prefix_exceeded")
    sessions = [
        s
        for s in (bgp_inventory.get("sessions") or [])
        if str(s.get("session_type") or "") == "ebgp"
        and s.get("local_device")
        and s.get("remote_device")
        and s.get("remote_ip")
    ]
    if not sessions:
        raise ValueError("bgp inventory has no eBGP sessions")
    originators = {
        str(item["device"])
        for item in (bgp_inventory.get("originated") or [])
        if item.get("device")
    }

    def _rank(sess: Mapping[str, Any]) -> tuple[int, str, str, str]:
        local = str(sess["local_device"])
        remote = str(sess["remote_device"])
        # Prefer peer (remote) as an originator so flood+withdraw hits its prefixes.
        peer_is_orig = 0 if remote in originators else 1
        either_orig = 0 if (local in originators or remote in originators) else 1
        return (peer_is_orig, either_orig, local, remote)

    sessions = sorted(
        sessions,
        key=lambda s: (
            *_rank(s),
            str(s.get("remote_ip") or ""),
        ),
    )
    seen: set[tuple[str, str]] = set()
    for sess in sessions:
        local = str(sess["local_device"])
        remote = str(sess["remote_device"])
        key = tuple(sorted((local, remote)))
        if key in seen:
            continue
        seen.add(key)
        # Orient so peer is the originator when possible.
        if remote in originators or local not in originators:
            return {
                "receiver_name": local,
                "peer_name": remote,
                "neighbor_ip": str(sess["remote_ip"]),
            }
        # local is originator and remote is not: flip using the reverse session.
        reverse = next(
            (
                s
                for s in sessions
                if str(s.get("local_device")) == remote
                and str(s.get("remote_device")) == local
                and s.get("remote_ip")
            ),
            None,
        )
        if reverse is not None:
            return {
                "receiver_name": remote,
                "peer_name": local,
                "neighbor_ip": str(reverse["remote_ip"]),
            }
        return {
            "receiver_name": local,
            "peer_name": remote,
            "neighbor_ip": str(sess["remote_ip"]),
        }
    sess = sessions[0]
    return {
        "receiver_name": str(sess["local_device"]),
        "peer_name": str(sess["remote_device"]),
        "neighbor_ip": str(sess["remote_ip"]),
    }


def _router_backbone_link(
    isp_inventory: Mapping[str, Any], router_device: str
) -> tuple[str, str] | None:
    """Return ``(router_device, iface)`` for the first backbone link involving ``router_device``."""
    for link in sorted(
        isp_inventory.get("links") or [],
        key=lambda item: str(item.get("link_id") or ""),
    ):
        for side in ("endpoint_a", "endpoint_b"):
            ep = link.get(side) or {}
            if ep.get("device") == router_device and ep.get("iface"):
                return str(router_device), str(ep["iface"])
    return None


def _originated_prefix_for_device(
    bgp_inventory: Mapping[str, Any] | None, router_device: str
) -> str | None:
    if not bgp_inventory:
        return None
    rows = sorted(
        (
            item
            for item in (bgp_inventory.get("originated") or [])
            if str(item.get("device") or "") == router_device and item.get("prefix")
        ),
        key=lambda item: str(item.get("prefix") or ""),
    )
    if not rows:
        return None
    return str(rows[0]["prefix"])


def isp_bgp_symptom_targets(
    isp_inventory: Mapping[str, Any],
    bgp_inventory: Mapping[str, Any] | None,
    router_device: str,
    problem: str,
    *,
    hijack_prefix: str | None = None,
) -> dict[str, str]:
    """Cross-PoP probe endpoints for BGP-originator faults on ``router_device``."""
    if problem == "bgp_blackhole_community_leak":
        observer = str(bgp_inventory.get("data_plane_observer_host") or "")
        ping_addr = str(bgp_inventory.get("target_ping_address") or "")
        leaker = str(bgp_inventory.get("leaker_device") or router_device)
        if observer and ping_addr:
            return {
                "symptom_host": observer,
                "probe_dst_ip": ping_addr,
                "peer_host": stub_host_for_router(leaker),
            }
        return {}

    prefix: str | None = None
    if problem == "bgp_missing_route_advertisement":
        prefix = _originated_prefix_for_device(bgp_inventory, router_device)
    elif problem == "bgp_hijacking":
        prefix = hijack_prefix or DEFAULT_HIJACK_PREFIX

    if prefix:
        net = ipaddress.ip_network(prefix, strict=False)
        dst = str(net.network_address + 1)
        # Prefer a lab-verified cross-AS observer for this prefix when present.
        observers = [
            str(item.get("observer") or "")
            for item in (bgp_inventory or {}).get("expect_reachable") or []
            if str(item.get("prefix") or "") == prefix and item.get("observer")
        ]
        remote = ""
        if observers:
            remote = sorted(observers)[0]
        if not remote:
            nodes = sorted(
                str(n["device"])
                for n in (isp_inventory.get("nodes") or [])
                if n.get("device") and str(n["device"]) != router_device
            )
            remote = nodes[0] if nodes else ""
        if remote:
            # Probe from the observer router (same source as lab
            # ``bgp_prefixes_propagated``). Stub hosts are not required for
            # this control-plane advertisement withdrawal signal.
            return {
                "symptom_host": remote,
                "probe_dst_ip": dst,
                "peer_host": stub_host_for_router(router_device),
            }
    return {}


def enrich_isp_symptom_params(
    params: dict[str, str],
    problem: str,
    isp_inventory: Mapping[str, Any],
    bgp_inventory: Mapping[str, Any] | None,
) -> None:
    """Attach symptom probe endpoints for ISP BGP faults when not already set."""
    if params.get("symptom_host") and params.get("probe_dst_ip"):
        return
    router = params.get("host_name")
    if not router:
        return
    if problem in BGP_ORIGINATOR_PROBLEMS or problem == BGP_HIJACK_PROBLEM:
        extra = isp_bgp_symptom_targets(
            isp_inventory,
            bgp_inventory,
            router,
            problem,
            hijack_prefix=params.get("target_network"),
        )
        for key, value in extra.items():
            params.setdefault(key, value)
    if problem == BGP_RTBH_LEAK_PROBLEM:
        extra = isp_bgp_symptom_targets(
            isp_inventory,
            bgp_inventory,
            router,
            problem,
        )
        for key, value in extra.items():
            params.setdefault(key, value)


def isp_inject_params(
    problem: str,
    isp_inventory: Mapping[str, Any],
    bgp_inventory: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build ``--set``-style inject overrides for an ISP-compatible problem."""
    if problem in LINK_INTF_PROBLEMS or problem in LINK_BACKBONE_PROBLEMS:
        device, iface = first_link_endpoint(isp_inventory)
        return {"host_name": device, "intf_name": iface}
    if problem in LINK_HOST_ONLY_PROBLEMS:
        return {"host_name": first_router(isp_inventory)}
    if problem in BGP_ORIGINATOR_PROBLEMS:
        host = first_originator(bgp_inventory)
        out: dict[str, str] = {"host_name": host}
        if problem == "bgp_missing_route_advertisement":
            prefix = _originated_prefix_for_device(bgp_inventory, host)
            if prefix:
                out["prefix"] = prefix
        return out
    if problem == BGP_HIJACK_PROBLEM:
        host, prefix = hijack_speaker_and_prefix(bgp_inventory)
        return {"host_name": host, "target_network": prefix}
    if problem == BGP_RPKI_LEAK_PROBLEM:
        if not bgp_inventory or not bgp_inventory.get("rpki"):
            raise ValueError(
                "bgp_rpki_invalid_route_leak requires ISP eBGP RPKI inventory"
            )
        leaker = bgp_inventory.get("leaker_device")
        if not leaker:
            raise ValueError("bgp inventory missing leaker_device")
        return {"host_name": str(leaker)}
    if problem == BGP_RTBH_LEAK_PROBLEM:
        if not bgp_inventory or not bgp_inventory.get("rtbh"):
            raise ValueError(
                "bgp_blackhole_community_leak requires ISP eBGP RTBH inventory"
            )
        leaker = bgp_inventory.get("leaker_device")
        if not leaker:
            raise ValueError("bgp inventory missing leaker_device")
        return {"host_name": str(leaker)}
    if problem == BGP_MAX_PREFIX_PROBLEM:
        return first_ebgp_session(bgp_inventory)
    raise ValueError(f"unsupported isp inject problem: {problem!r}")
