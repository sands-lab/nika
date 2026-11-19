"""Match current candidates against the frozen 0.2.0 release identities."""

import json
from functools import lru_cache
from pathlib import Path

from nika.net_env.net_env_pool import list_all_net_envs
from nika.problems.registry import resolve_problem_name
from nika.workflows.benchmark.network_identity import network_identity

# Taxonomy merges and renames that are not registry aliases. None means deleted.
FAULT_MAP = {
    "dhcp_spoofed_gateway": "dhcp_option_gateway",
    "dhcp_spoofed_dns": "dhcp_option_dns",
    "dhcp_spoofed_subnet": "dhcp_option_subnet",
    "ospf_neighbor_missing": "ospf_interface_not_enabled",
    "k8s_clusterip_routing_broken": "k8s_node_firewall_drop",
    "p4_ecn_threshold_misconfiguration": "p4_ecn_marking_disabled",
    "link_detach": "link_down",
    "sender_resource_contention": "endpoint_cpu_contention",
    "receiver_resource_contention": "endpoint_cpu_contention",
    "incast_traffic_network_limitation": "link_capacity_bottleneck",
    "icmp_acl_block": "host_acl_block",
    "http_acl_block": "host_acl_block",
    "k8s_coredns_isolated": "dns_port_blocked",
    "p4_tcam_entry_corruption": "p4_table_entry_misconfig",
    "bgp_rpki_invalid_route_leak": "bgp_hijacking",
    "bgp_max_prefix_exceeded": None,
    "int_insufficient_mtu_headroom": None,
    "load_balancer_overload": None,
    "dns_lookup_latency": None,
}


def canonical_fault(fault):
    return FAULT_MAP.get(fault, resolve_problem_name(fault))


@lru_cache(maxsize=1)
def legacy_index():
    rows = json.loads(Path(__file__).with_name("legacy_020.json").read_text())
    index = {}
    scenarios = list_all_net_envs()
    for row in rows:
        if row["scenario"] not in scenarios:
            continue
        fault = canonical_fault(row["problem"])
        if fault is not None:
            key = (row["scenario"], network_identity(row), fault)
            index.setdefault(key, []).append(row)
    return index


def match(row, split, index=None):
    """Return (score, match type, same side) for the best legacy match."""
    if index is None:
        index = legacy_index()
    key = (row["scenario"], network_identity(row), canonical_fault(row["problem"]))
    best = (0, None, False)
    for old in index.get(key, []):
        # An exact placement has the same explicitly specified deployment and
        # injection parameters. Extra generated parameters do not change it.
        variants = ("topo_size", "igp", "bgp_mode", "rpki", "backend", "device_profile")
        same_variant = all(row.get(k) == old[k] for k in variants if k in old)
        old_inject, new_inject = old.get("inject", {}), row.get("inject", {})
        same_inject = (
            all(k in new_inject and new_inject[k] == v for k, v in old_inject.items())
            if old_inject
            else not new_inject
        )
        # Merged fault variants must retain the legacy behavior.
        extra = {
            "icmp_acl_block": ("protocol", "icmp"),
            "http_acl_block": ("protocol", "http"),
            "bgp_rpki_invalid_route_leak": ("variant", "rpki_invalid_origin"),
        }.get(old["problem"])
        if extra and new_inject.get(extra[0]) != extra[1]:
            same_inject = False
        exact = same_variant and same_inject
        same_side = old["split"] == split
        score = (3 if exact else 1) + int(same_side)
        if score > best[0]:
            best = (score, "exact" if exact else "network_fault", same_side)
    return best
