"""Fixed Abilene eBGP inter-AS / RPKI peer-role profile.

Activated only for topology ``abilene`` + ``bgp_mode=ebgp``. Keeps the
generic 3-AS partition and overlays deterministic roles, intra-AS iBGP,
leak-target prefixes, and ROV observer wiring.
"""

from __future__ import annotations

from dataclasses import replace
from ipaddress import IPv4Network
from typing import Any

from nika.net_env.isp.bgp.config import EBGP_BASE_ASN
from nika.net_env.isp.bgp.plan import (
    BgpNodePlan,
    BgpOriginatedPrefix,
    BgpPlan,
    BgpSession,
)
from nika.net_env.isp.igp.plan import IspPlan

PROFILE_NAME = "abilene_inter_as_rpki"
TOPOLOGY_NAME = "abilene"

# Roles (ASNs follow generic eBGP partition of sorted Abilene devices).
LEGITIMATE_ORIGIN_ASN = EBGP_BASE_ASN  # 65001
LEAKER_ASN = EBGP_BASE_ASN + 1  # 65002
ROV_OBSERVER_ASN = EBGP_BASE_ASN + 2  # 65003

LEAKER_DEVICE = "losang"
ROV_OBSERVER = "snvang"
NON_ROV_OBSERVER = "atlang"

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


def is_abilene_ebgp_rpki(topology_name: str, mode: str) -> bool:
    return topology_name == TOPOLOGY_NAME and mode == "ebgp"


def leak_ping_address(prefix: str) -> str:
    net = IPv4Network(prefix)
    return str(net.network_address + 1)


def apply_abilene_inter_as_profile(plan: BgpPlan, isp_plan: IspPlan) -> BgpPlan:
    """Overlay Abilene RPKI inter-AS roles, iBGP meshes, and leak prefixes."""
    if not is_abilene_ebgp_rpki(plan.topology_name, plan.mode):
        return plan

    loopbacks = {n.device_name: n.loopback for n in isp_plan.nodes}
    asn_of = {n.device_name: n.asn for n in plan.nodes}
    members: dict[int, list[str]] = {}
    for device, asn in asn_of.items():
        members.setdefault(asn, []).append(device)
    for asn in members:
        members[asn] = sorted(members[asn])

    if LEAKER_DEVICE not in asn_of or asn_of[LEAKER_DEVICE] != LEAKER_ASN:
        raise ValueError(
            f"Abilene inter-AS profile expects {LEAKER_DEVICE!r} in ASN "
            f"{LEAKER_ASN}, got {asn_of.get(LEAKER_DEVICE)!r}."
        )
    if ROV_OBSERVER not in asn_of or asn_of[ROV_OBSERVER] != ROV_OBSERVER_ASN:
        raise ValueError(
            f"Abilene inter-AS profile expects {ROV_OBSERVER!r} in ASN "
            f"{ROV_OBSERVER_ASN}, got {asn_of.get(ROV_OBSERVER)!r}."
        )
    if NON_ROV_OBSERVER not in asn_of:
        raise ValueError(
            f"Abilene inter-AS profile missing non-ROV observer {NON_ROV_OBSERVER!r}."
        )

    sessions = list(plan.sessions)
    for asn, group in members.items():
        if len(group) < 2:
            continue
        for i, left in enumerate(group):
            for right in group[i + 1 :]:
                sessions.append(
                    BgpSession(
                        local_device=left,
                        remote_device=right,
                        local_ip=loopbacks[left],
                        remote_ip=loopbacks[right],
                        local_asn=asn,
                        remote_asn=asn,
                        session_type="ibgp",
                        update_source="lo",
                    )
                )
                sessions.append(
                    BgpSession(
                        local_device=right,
                        remote_device=left,
                        local_ip=loopbacks[right],
                        remote_ip=loopbacks[left],
                        local_asn=asn,
                        remote_asn=asn,
                        session_type="ibgp",
                        update_source="lo",
                    )
                )

    leak_originated = [
        BgpOriginatedPrefix(
            device=LEAKER_DEVICE,
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
        if node.device_name == LEAKER_DEVICE:
            for role in ("leaker", "originator"):
                if role not in roles:
                    roles.append(role)
        if node.device_name == ROV_OBSERVER and "rov_observer" not in roles:
            roles.append("rov_observer")
        if node.device_name == NON_ROV_OBSERVER and "non_rov_observer" not in roles:
            roles.append("non_rov_observer")

        export_deny = LEAK_PREFIXES if node.asn == LEAKER_ASN else ()
        rov = node.device_name == ROV_OBSERVER
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
        mode=plan.mode,
        topology_name=plan.topology_name,
        nodes=nodes,
        sessions=sessions_t,
        originated=originated,
        expect_reachable=plan.expect_reachable,
        deny_prefixes=plan.deny_prefixes,
    )
    refreshed.update(_profile_inventory(nodes, sessions_t, originated))

    return BgpPlan(
        mode=plan.mode,
        topology_name=plan.topology_name,
        nodes=tuple(nodes),
        sessions=sessions_t,
        originated=originated,
        expect_reachable=plan.expect_reachable,
        deny_prefixes=plan.deny_prefixes,
        inventory=refreshed,
    )


def _profile_inventory(
    nodes: list[BgpNodePlan],
    sessions: tuple[BgpSession, ...],
    originated: tuple[BgpOriginatedPrefix, ...],
) -> dict[str, Any]:
    return {
        "inter_as_profile": PROFILE_NAME,
        "rpki": True,
        "legitimate_origin_asn": LEGITIMATE_ORIGIN_ASN,
        "leaker_asn": LEAKER_ASN,
        "leaker_device": LEAKER_DEVICE,
        "rov_observer": ROV_OBSERVER,
        "non_rov_observer": NON_ROV_OBSERVER,
        "leak_prefixes": list(LEAK_PREFIXES),
        "rpki_rtr": {
            "machine": ROUTINATOR_MACHINE,
            "address": RPKI_ROUTINATOR_ADDRESS,
            "port": RPKI_RTR_PORT,
            "router": ROV_OBSERVER,
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
            "comment": f"NIKA Abilene VRP for {prefix}",
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
