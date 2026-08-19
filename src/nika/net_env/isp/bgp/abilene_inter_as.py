"""Compatibility re-exports for the topology-agnostic RPKI profile.

Prefer importing from :mod:`nika.net_env.isp.bgp.rpki_profile`.
"""

from __future__ import annotations

from nika.net_env.isp.bgp.rpki_profile import (
    LEAK_PREFIXES,
    LEAKER_ASN,
    LEGITIMATE_ORIGIN_ASN,
    PROFILE_NAME,
    ROV_OBSERVER_ASN,
    RPKI_COLLISION_DOMAIN,
    RPKI_PREFIXLEN,
    RPKI_ROUTER_ADDRESS,
    RPKI_ROUTINATOR_ADDRESS,
    RPKI_RTR_PORT,
    ROUTINATOR_MACHINE,
    apply_rpki_profile,
    leak_ping_address,
    slurm_document,
)

__all__ = [
    "LEAK_PREFIXES",
    "LEAKER_ASN",
    "LEGITIMATE_ORIGIN_ASN",
    "PROFILE_NAME",
    "ROV_OBSERVER_ASN",
    "RPKI_COLLISION_DOMAIN",
    "RPKI_PREFIXLEN",
    "RPKI_ROUTER_ADDRESS",
    "RPKI_ROUTINATOR_ADDRESS",
    "RPKI_RTR_PORT",
    "ROUTINATOR_MACHINE",
    "apply_rpki_profile",
    "leak_ping_address",
    "slurm_document",
]
