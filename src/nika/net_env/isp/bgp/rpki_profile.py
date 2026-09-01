"""Topology-agnostic eBGP RPKI / ROV profile for ISP labs.

Overlays deterministic RPKI roles, leak-target prefixes, and ROV observer
wiring onto a compiled eBGP plan. Used when ``isp`` enables the RPKI
capability (``bgp_mode=ebgp`` + ``rpki=true``).
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

PROFILE_NAME = "isp_rpki"

# Roles (ASNs follow generic eBGP partition: seed order → ASN index).
LEGITIMATE_ORIGIN_ASN = EBGP_BASE_ASN  # 65001
LEAKER_ASN = EBGP_BASE_ASN + 1  # 65002
ROV_OBSERVER_ASN = EBGP_BASE_ASN + 2  # 65003

# VRP-authorized for LEGITIMATE_ORIGIN_ASN only; leaker originates after inject.
LEAK_PREFIXES: tuple[str, ...] = ("203.0.113.0/24", "203.0.114.0/24")

# Routinator RTR attachment on the ROV observer (lab-local /30).
RPKI_COLLISION_DOMAIN = "RPKI"
# Outside edge-stub pool 10.254.0.0/16 and infra 10.0.0.0/8 business links.
RPKI_ROUTER_ADDRESS = "10.255.254.1"
RPKI_ROUTINATOR_ADDRESS = "10.255.254.2"
RPKI_PREFIXLEN = 30
RPKI_RTR_PORT = 3323
ROUTINATOR_MACHINE = "routinator"


def leak_ping_address(prefix: str) -> str:
    net = IPv4Network(prefix)
    return str(net.network_address + 1)


def apply_rpki_profile(plan: BgpPlan) -> BgpPlan:
    """Overlay RPKI roles and leak prefixes onto a compiled eBGP plan."""
    asn_of = {n.device_name: n.asn for n in plan.nodes}
    asns = sorted(set(asn_of.values()))
    if len(asns) < 3:
        raise BgpConfigError(
            f"RPKI profile requires at least three eBGP AS regions (got {len(asns)}).",
            topology=plan.topology_name,
        )
    if LEGITIMATE_ORIGIN_ASN not in asns:
        raise BgpConfigError(
            f"RPKI profile expects ASN {LEGITIMATE_ORIGIN_ASN} in the eBGP "
            f"partition (got {asns}).",
            topology=plan.topology_name,
        )
    if LEAKER_ASN not in asns or ROV_OBSERVER_ASN not in asns:
        raise BgpConfigError(
            f"RPKI profile expects ASNs {LEAKER_ASN} and {ROV_OBSERVER_ASN} "
            f"in the eBGP partition (got {asns}).",
            topology=plan.topology_name,
        )

    members: dict[int, list[str]] = defaultdict(list)
    for device, asn in asn_of.items():
        members[asn].append(device)
    for asn in members:
        members[asn].sort()

    ebgp_by_local: dict[str, list[BgpSession]] = defaultdict(list)
    as_adj: dict[int, set[int]] = defaultdict(set)
    for sess in plan.sessions:
        if sess.session_type != "ebgp":
            continue
        ebgp_by_local[sess.local_device].append(sess)
        as_adj[sess.local_asn].add(sess.remote_asn)

    leaker_device = _pick_leaker(members[LEAKER_ASN], ebgp_by_local)
    rov_observer = _pick_rov_observer(
        members[ROV_OBSERVER_ASN], ebgp_by_local, LEAKER_ASN
    )
    non_rov_observer = _pick_non_rov_observer(
        members[LEGITIMATE_ORIGIN_ASN], ebgp_by_local, LEAKER_ASN
    )
    _assert_diagnosable(
        plan.topology_name,
        as_adj=as_adj,
        leaker_device=leaker_device,
        rov_observer=rov_observer,
        non_rov_observer=non_rov_observer,
        asn_of=asn_of,
    )

    sessions = list(plan.sessions)
    leak_originated = [
        BgpOriginatedPrefix(
            device=leaker_device,
            prefix=prefix,
            ping_address=leak_ping_address(prefix),
        )
        for prefix in LEAK_PREFIXES
    ]
    originated = tuple(plan.originated) + tuple(leak_originated)

    by_dev_sessions: dict[str, list[BgpSession]] = {
        n.device_name: [] for n in plan.nodes
    }
    for sess in sessions:
        by_dev_sessions[sess.local_device].append(sess)
    by_dev_orig: dict[str, list[BgpOriginatedPrefix]] = {
        n.device_name: [] for n in plan.nodes
    }
    for pref in originated:
        by_dev_orig[pref.device].append(pref)

    nodes: list[BgpNodePlan] = []
    for node in plan.nodes:
        roles = list(node.roles)
        if node.asn == LEGITIMATE_ORIGIN_ASN and "legitimate_origin" not in roles:
            roles.append("legitimate_origin")
        if node.device_name == leaker_device:
            for role in ("leaker", "originator"):
                if role not in roles:
                    roles.append(role)
        if node.device_name == rov_observer and "rov_observer" not in roles:
            roles.append("rov_observer")
        if node.device_name == non_rov_observer and "non_rov_observer" not in roles:
            roles.append("non_rov_observer")

        export_deny = LEAK_PREFIXES if node.asn == LEAKER_ASN else ()
        rov = node.device_name == rov_observer
        rpki_cache = (RPKI_ROUTINATOR_ADDRESS, RPKI_RTR_PORT) if rov else None
        nodes.append(
            replace(
                node,
                roles=tuple(roles),
                sessions=tuple(by_dev_sessions[node.device_name]),
                originated=tuple(by_dev_orig[node.device_name]),
                rov_reject_invalid=rov,
                rpki_cache=rpki_cache,
                export_deny_prefixes=export_deny,
            )
        )

    sessions_t = tuple(sessions)
    from nika.net_env.isp.bgp.plan import _inventory

    refreshed = _inventory(
        mode="ebgp",
        topology_name=plan.topology_name,
        nodes=nodes,
        sessions=sessions_t,
        originated=originated,
        expect_reachable=plan.expect_reachable,
        deny_prefixes=plan.deny_prefixes,
    )
    refreshed.update(
        _profile_inventory(
            nodes,
            leaker_device=leaker_device,
            rov_observer=rov_observer,
            non_rov_observer=non_rov_observer,
        )
    )

    return BgpPlan(
        mode="ebgp",
        topology_name=plan.topology_name,
        nodes=tuple(nodes),
        sessions=sessions_t,
        originated=originated,
        expect_reachable=plan.expect_reachable,
        deny_prefixes=plan.deny_prefixes,
        inventory=refreshed,
    )


def _pick_leaker(members: list[str], ebgp_by_local: dict[str, list[BgpSession]]) -> str:
    borders = [d for d in members if ebgp_by_local.get(d)]
    pool = borders if borders else members
    return sorted(pool)[0]


def _pick_rov_observer(
    members: list[str],
    ebgp_by_local: dict[str, list[BgpSession]],
    leaker_asn: int,
) -> str:
    peer_leaker = [
        d
        for d in members
        if any(s.remote_asn == leaker_asn for s in ebgp_by_local.get(d, []))
    ]
    if peer_leaker:
        return sorted(peer_leaker)[0]
    borders = [d for d in members if ebgp_by_local.get(d)]
    pool = borders if borders else members
    return sorted(pool)[0]


def _pick_non_rov_observer(
    members: list[str],
    ebgp_by_local: dict[str, list[BgpSession]],
    leaker_asn: int,
) -> str:
    peer_leaker = [
        d
        for d in members
        if any(s.remote_asn == leaker_asn for s in ebgp_by_local.get(d, []))
    ]
    if peer_leaker:
        return sorted(peer_leaker)[0]
    borders = [d for d in members if ebgp_by_local.get(d)]
    pool = borders if borders else members
    return sorted(pool)[0]


def _assert_diagnosable(
    topology_name: str,
    *,
    as_adj: dict[int, set[int]],
    leaker_device: str,
    rov_observer: str,
    non_rov_observer: str,
    asn_of: dict[str, int],
) -> None:
    leaker_asn = asn_of[leaker_device]
    rov_asn = asn_of[rov_observer]
    non_rov_asn = asn_of[non_rov_observer]
    if rov_asn == leaker_asn or non_rov_asn == leaker_asn:
        raise BgpConfigError(
            "RPKI profile requires ROV and non-ROV observers outside the "
            f"leaker AS {leaker_asn}.",
            topology=topology_name,
        )
    if not as_adj.get(leaker_asn):
        raise BgpConfigError(
            f"RPKI profile leaker AS {leaker_asn} has no eBGP edge outward.",
            topology=topology_name,
        )
    reachable = _as_reachable(as_adj, leaker_asn)
    missing = [asn for asn in (rov_asn, non_rov_asn) if asn not in reachable]
    if missing:
        raise BgpConfigError(
            "RPKI profile cannot propagate leak routes from leaker AS "
            f"{leaker_asn} to observer AS(es) {missing}.",
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
    nodes: list[BgpNodePlan],
    *,
    leaker_device: str,
    rov_observer: str,
    non_rov_observer: str,
) -> dict[str, Any]:
    return {
        "inter_as_profile": PROFILE_NAME,
        "rpki": True,
        "legitimate_origin_asn": LEGITIMATE_ORIGIN_ASN,
        "leaker_asn": LEAKER_ASN,
        "leaker_device": leaker_device,
        "rov_observer": rov_observer,
        "non_rov_observer": non_rov_observer,
        "leak_prefixes": list(LEAK_PREFIXES),
        "rpki_rtr": {
            "machine": ROUTINATOR_MACHINE,
            "address": RPKI_ROUTINATOR_ADDRESS,
            "port": RPKI_RTR_PORT,
            "router": rov_observer,
            "router_address": RPKI_ROUTER_ADDRESS,
            "prefixlen": RPKI_PREFIXLEN,
            "collision_domain": RPKI_COLLISION_DOMAIN,
        },
        "leaker_as_devices": sorted(
            n.device_name for n in nodes if n.asn == LEAKER_ASN
        ),
    }


def slurm_document() -> dict[str, Any]:
    """Offline SLURM assertions: leak prefixes authorized only for origin ASN."""
    assertions = [
        {
            "asn": LEGITIMATE_ORIGIN_ASN,
            "prefix": prefix,
            "maxPrefixLength": int(prefix.split("/")[1]),
            "comment": f"NIKA offline VRP for {prefix}",
        }
        for prefix in LEAK_PREFIXES
    ]
    return {
        "slurmVersion": 1,
        "validationOutputFilters": {
            "prefixFilters": [],
            "bgpsecFilters": [],
        },
        "locallyAddedAssertions": {
            "prefixAssertions": assertions,
            "bgpsecAssertions": [],
        },
    }
