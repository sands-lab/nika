"""Original 0.1.0 labs, resolvable by ID but absent from ``list_all_net_envs``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nika.net_env.net_env_pool import NetEnvSpec


def scenario_specs() -> dict[str, NetEnvSpec]:
    from nika.net_env.net_env_pool import NetEnvSpec

    base = "nika.net_env.compat.v010"
    sizes = ["s", "m", "l"]
    return {
        "rip_small_internet_vpn": NetEnvSpec(
            lab_name="rip_small_internet_vpn",
            module=f"{base}.rip_small_internet_vpn.lab",
            class_name="RIPSmallInternetVPN",
            tags=("link", "http", "pc", "frr", "mac", "arp", "vpn", "icmp"),
            supported_backends=("kathara",),
            topo_size=sizes,
        ),
        "sdn_clos": NetEnvSpec(
            lab_name="sdn_clos",
            module=f"{base}.sdn_clos.lab",
            class_name="SDNClos",
            tags=("link", "sdn", "pc", "mac", "arp", "icmp"),
            supported_backends=("kathara",),
            topo_size=sizes,
        ),
        "sdn_star": NetEnvSpec(
            lab_name="sdn_star",
            module=f"{base}.sdn_star.lab",
            class_name="SDNStar",
            tags=("link", "sdn", "pc", "mac", "arp", "icmp"),
            supported_backends=("kathara",),
            topo_size=sizes,
        ),
        "p4_bloom_filter": NetEnvSpec(
            lab_name="p4_bloom_filter",
            module=f"{base}.p4_bloom_filter.lab",
            class_name="P4BloomFilter",
            tags=("link", "pc", "p4", "mac", "arp", "icmp", "bloom_filter"),
            supported_backends=("kathara",),
        ),
        "p4_counter": NetEnvSpec(
            lab_name="p4_counter",
            module=f"{base}.p4_counter.lab",
            class_name="P4Counter",
            tags=("link", "pc", "p4", "mac", "arp", "icmp"),
            supported_backends=("kathara",),
        ),
        "p4_mpls": NetEnvSpec(
            lab_name="p4_mpls",
            module=f"{base}.p4_mpls.lab",
            class_name="P4_MPLS",
            tags=("link", "pc", "p4", "mac", "arp", "icmp", "mpls"),
            supported_backends=("kathara",),
        ),
    }
