"""Topology-agnostic eBGP RTBH profile for ISP labs.

Overlays deterministic inter-AS roles, provider blackhole community policy,
and observer wiring onto a compiled eBGP plan. Named RTBH scenarios enable it
through ``compile_bgp_plan(..., rtbh=True)``; current eBGP deployments compose
it with the RPKI profile.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from ipaddress import IPv4Network
from typing import Any

from nika.net_env.isp.bgp.config import EBGP_BASE_ASN
from nika.net_env.isp.bgp.errors import BgpConfigError
from nika.net_env.isp.bgp.plan import (
    BgpNodePlan,
    BgpOriginatedPrefix,
    BgpPlan,
    BgpSession,
)


LEGITIMATE_ORIGIN_ASN = EBGP_BASE_ASN  # 65001
LEAKER_ASN = EBGP_BASE_ASN + 1  # 65002
RTBH_PROVIDER_ASN = EBGP_BASE_ASN + 2  # 65003

TARGET_PREFIX = "198.51.100.0/24"
BLACKHOLE_COMMUNITY = (RTBH_PROVIDER_ASN, 666)
DISCARD_NH = "192.0.2.1"
LEAKER_OUTBOUND_ROUTE_MAP = f"BGP-OUT-TO-{RTBH_PROVIDER_ASN}"


def target_ping_address(prefix: str = TARGET_PREFIX) -> str:
    net = IPv4Network(prefix)
    return str(net.network_address + 1)


def rtbh_leaker(inventory: dict[str, Any]) -> str:
    """The RTBH leaker router of a BGP inventory with the RTBH profile."""
    return str(
        inventory.get("rtbh_leaker_device") or inventory.get("leaker_device") or ""
    )


def provider_edges(plan: BgpPlan) -> list[tuple[str, str]]:
    """Sorted ``(leaker, provider)`` eBGP edges usable by the RTBH profile.

    The provider must also peer the legitimate origin AS. Its healthy route
    to the target then leaves over that session, whose link address the
    origin AS routes, so the provider observes the target from its own
    address before and after the blackhole.
    """
    ebgp = [sess for sess in plan.sessions if sess.session_type == "ebgp"]
    peers_origin = {
        sess.local_device
        for sess in ebgp
        if sess.local_asn == RTBH_PROVIDER_ASN
        and sess.remote_asn == LEGITIMATE_ORIGIN_ASN
    }
    return sorted(
        (sess.local_device, sess.remote_device)
        for sess in ebgp
        if sess.local_asn == LEAKER_ASN
        and sess.remote_asn == RTBH_PROVIDER_ASN
        and sess.remote_device in peers_origin
    )


def apply_rtbh_profile(plan: BgpPlan) -> BgpPlan:
    """Overlay RTBH roles and provider import policy onto a compiled eBGP plan.

    On a plan that already carries the RPKI profile, the RPKI inventory is
    kept and the RTBH leaker is recorded as ``rtbh_leaker_device`` (see
    :func:`rtbh_leaker`), since the two profiles pick their leakers apart.
    """
    asn_of = {n.device_name: n.asn for n in plan.nodes}
    asns = sorted(set(asn_of.values()))
    if len(asns) < 3:
        raise BgpConfigError(
            f"RTBH profile requires at least three eBGP AS regions (got {len(asns)}).",
            topology=plan.topology_name,
        )
    for required in (LEGITIMATE_ORIGIN_ASN, LEAKER_ASN, RTBH_PROVIDER_ASN):
        if required not in asns:
            raise BgpConfigError(
                f"RTBH profile expects ASN {required} in the eBGP partition "
                f"(got {asns}).",
                topology=plan.topology_name,
            )

    ebgp_by_local: dict[str, list[BgpSession]] = defaultdict(list)
    as_adj: dict[int, set[int]] = defaultdict(set)
    for sess in plan.sessions:
        if sess.session_type != "ebgp":
            continue
        ebgp_by_local[sess.local_device].append(sess)
        as_adj[sess.local_asn].add(sess.remote_asn)

    origin_rows = sorted(
        (
            pref
            for pref in plan.originated
            if asn_of.get(pref.device) == LEGITIMATE_ORIGIN_ASN
        ),
        key=lambda item: item.prefix,
    )
    if not origin_rows:
        raise BgpConfigError(
            "RTBH profile requires a legitimate origin prefix in AS "
            f"{LEGITIMATE_ORIGIN_ASN}.",
            topology=plan.topology_name,
        )
    target = origin_rows[0]
    if target.prefix != TARGET_PREFIX:
        raise BgpConfigError(
            f"RTBH profile expects target prefix {TARGET_PREFIX!r} "
            f"(got {target.prefix!r}).",
            topology=plan.topology_name,
        )

    edges = provider_edges(plan)
    if not edges:
        raise BgpConfigError(
            f"RTBH profile found no eBGP edge from AS{LEAKER_ASN} to an "
            f"AS{RTBH_PROVIDER_ASN} router that peers AS{LEGITIMATE_ORIGIN_ASN}.",
            topology=plan.topology_name,
        )
    leaker_device, rtbh_provider = edges[0]
    legitimate_origin_device = target.device
    leaker_to_rtbh_neighbor_ip = _leaker_to_rtbh_neighbor_ip(
        leaker_device, rtbh_provider, ebgp_by_local
    )
    _assert_diagnosable(
        plan.topology_name,
        as_adj=as_adj,
        leaker_device=leaker_device,
        rtbh_provider=rtbh_provider,
        legitimate_origin_device=legitimate_origin_device,
        asn_of=asn_of,
    )

    nodes: list[BgpNodePlan] = []
    for node in plan.nodes:
        roles = list(node.roles)
        if node.asn == LEGITIMATE_ORIGIN_ASN and "legitimate_origin" not in roles:
            roles.append("legitimate_origin")
        if node.device_name == leaker_device and "leaker" not in roles:
            roles.append("leaker")
        if node.device_name == rtbh_provider and "rtbh_provider" not in roles:
            roles.append("rtbh_provider")

        rtbh_import = node.device_name == rtbh_provider
        outbound_maps: tuple[tuple[str, str], ...] = ()
        if node.device_name == leaker_device:
            outbound_maps = ((leaker_to_rtbh_neighbor_ip, LEAKER_OUTBOUND_ROUTE_MAP),)

        nodes.append(
            replace(
                node,
                roles=tuple(roles),
                rtbh_import_policy=rtbh_import,
                ebgp_outbound_route_maps=outbound_maps,
            )
        )

    sessions_t = tuple(plan.sessions)
    from nika.net_env.isp.bgp.plan import _inventory

    refreshed = _inventory(
        mode="ebgp",
        topology_name=plan.topology_name,
        nodes=nodes,
        sessions=sessions_t,
        originated=plan.originated,
        expect_reachable=plan.expect_reachable,
        deny_prefixes=plan.deny_prefixes,
    )
    # Keep another profile's inventory (the RPKI roles) when composing.
    refreshed.update(
        {key: value for key, value in plan.inventory.items() if key not in refreshed}
    )
    composed = bool(plan.inventory.get("rpki"))
    refreshed.update(
        _profile_inventory(
            target=target,
            leaker_device=leaker_device,
            rtbh_provider=rtbh_provider,
            legitimate_origin_device=legitimate_origin_device,
            leaker_to_rtbh_neighbor_ip=leaker_to_rtbh_neighbor_ip,
        )
    )
    if composed:
        refreshed["rtbh_leaker_device"] = leaker_device
        refreshed["leaker_device"] = plan.inventory["leaker_device"]

    return BgpPlan(
        mode="ebgp",
        topology_name=plan.topology_name,
        nodes=tuple(nodes),
        sessions=sessions_t,
        originated=plan.originated,
        expect_reachable=plan.expect_reachable,
        deny_prefixes=plan.deny_prefixes,
        inventory=refreshed,
    )


def _leaker_to_rtbh_neighbor_ip(
    leaker_device: str,
    rtbh_provider: str,
    ebgp_by_local: dict[str, list[BgpSession]],
) -> str:
    for sess in ebgp_by_local.get(leaker_device, []):
        if sess.remote_device == rtbh_provider and sess.remote_ip:
            return str(sess.remote_ip)
    raise BgpConfigError(
        f"RTBH profile found no eBGP session from leaker {leaker_device!r} "
        f"to provider {rtbh_provider!r}."
    )


def _assert_diagnosable(
    topology_name: str,
    *,
    as_adj: dict[int, set[int]],
    leaker_device: str,
    rtbh_provider: str,
    legitimate_origin_device: str,
    asn_of: dict[str, int],
) -> None:
    leaker_asn = asn_of[leaker_device]
    provider_asn = asn_of[rtbh_provider]
    origin_asn = asn_of[legitimate_origin_device]
    if provider_asn == leaker_asn or origin_asn == leaker_asn:
        raise BgpConfigError(
            "RTBH profile requires origin and provider ASes outside the leaker AS "
            f"{leaker_asn}.",
            topology=topology_name,
        )
    if not as_adj.get(leaker_asn):
        raise BgpConfigError(
            f"RTBH profile leaker AS {leaker_asn} has no eBGP edge outward.",
            topology=topology_name,
        )
    reachable = _as_reachable(as_adj, leaker_asn)
    for asn in (provider_asn, origin_asn):
        if asn not in reachable:
            raise BgpConfigError(
                f"RTBH profile cannot propagate routes from leaker AS {leaker_asn} "
                f"to AS {asn}.",
                topology=topology_name,
            )


def _as_reachable(as_adj: dict[int, set[int]], source: int) -> set[int]:
    seen = {source}
    queue: deque[int] = deque([source])
    while queue:
        current = queue.popleft()
        for neighbor in as_adj.get(current, ()):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def _profile_inventory(
    *,
    target: BgpOriginatedPrefix,
    leaker_device: str,
    rtbh_provider: str,
    legitimate_origin_device: str,
    leaker_to_rtbh_neighbor_ip: str,
) -> dict[str, Any]:
    community = f"{BLACKHOLE_COMMUNITY[0]}:{BLACKHOLE_COMMUNITY[1]}"
    return {
        "rtbh": True,
        "legitimate_origin_asn": LEGITIMATE_ORIGIN_ASN,
        "leaker_asn": LEAKER_ASN,
        "rtbh_provider_asn": RTBH_PROVIDER_ASN,
        "leaker_device": leaker_device,
        "rtbh_provider_device": rtbh_provider,
        "legitimate_origin_device": legitimate_origin_device,
        "target_prefix": target.prefix,
        "target_ping_address": target.ping_address,
        "blackhole_community": community,
        "leaker_to_rtbh_neighbor_ip": leaker_to_rtbh_neighbor_ip,
        "leaker_outbound_route_map": LEAKER_OUTBOUND_ROUTE_MAP,
        "discard_next_hop": DISCARD_NH,
        "data_plane_observer_host": rtbh_provider,
    }


def rtbh_design_note(inventory: dict[str, Any]) -> str:
    """Brief sentence on the provider's remote-triggered blackholing policy."""
    return (
        f" {inventory.get('rtbh_provider_device')} (AS{RTBH_PROVIDER_ASN}) discards"
        " traffic to business prefixes that its eBGP neighbors tag with the"
        f" blackhole community {inventory.get('blackhole_community')} (RFC 7999)."
    )
