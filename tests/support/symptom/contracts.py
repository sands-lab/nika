"""Per-failure symptom contracts for the test-path evaluate_symptom API.

Production inject uses only artifact ``verify_fault``. Scenario probe paths used
by inject (host pools) are declared by each scenario (``nika.net_env.net_env_pool.get_probe_path``).
"""

from __future__ import annotations

from dataclasses import dataclass

from nika.net_env.net_env_pool import get_probe_path
from tests.support.symptom.types import ProbeKind, SymptomClass

# Re-export for tests that need scenario paths via the same package.
__all__ = [
    "SymptomContract",
    "get_symptom_contract",
    "list_symptom_contracts",
    "get_probe_path",
]


@dataclass(frozen=True)
class SymptomContract:
    failure: str
    symptom_class: SymptomClass
    probe: ProbeKind
    loss_min_percent: float = 10.0
    latency_factor: float = 2.0


def _c(
    failure: str,
    symptom_class: SymptomClass,
    probe: ProbeKind,
    *,
    loss_min: float = 10.0,
) -> SymptomContract:
    return SymptomContract(
        failure=failure,
        symptom_class=symptom_class,
        probe=probe,
        loss_min_percent=loss_min,
    )


_SYMPTOM_CONTRACTS: dict[str, SymptomContract] = {
    "link_down": _c("link_down", "unreachable", "path_ping"),
    "link_detach": _c("link_detach", "unreachable", "path_ping"),
    "link_flap": _c("link_flap", "loss", "custom"),
    "link_packet_corruption": _c("link_packet_corruption", "degradation", "custom"),
    "silent_egress_packet_loss": _c(
        "silent_egress_packet_loss", "gray", "custom", loss_min=1.0
    ),
    "bgp_asn_misconfig": _c("bgp_asn_misconfig", "control_plane", "control_plane_bgp"),
    "bgp_max_prefix_exceeded": _c(
        "bgp_max_prefix_exceeded", "control_plane", "control_plane_bgp"
    ),
    "bgp_blackhole_community_leak": _c(
        "bgp_blackhole_community_leak", "unreachable", "path_ping"
    ),
    "bgp_rpki_invalid_route_leak": _c(
        "bgp_rpki_invalid_route_leak", "control_plane", "path_ping"
    ),
    "bgp_missing_route_advertisement": _c(
        "bgp_missing_route_advertisement", "unreachable", "path_ping"
    ),
    "frr_service_down": _c(
        "frr_service_down", "control_plane", "control_plane_routing"
    ),
    "ospf_area_misconfiguration": _c(
        "ospf_area_misconfiguration", "control_plane", "control_plane_ospf"
    ),
    "ospf_neighbor_missing": _c(
        "ospf_neighbor_missing", "control_plane", "control_plane_ospf"
    ),
    "device_forwarding_packet_corruption": _c(
        "device_forwarding_packet_corruption", "gray", "custom"
    ),
    "mtu_mismatch": _c("mtu_mismatch", "unreachable", "path_mtu_frag_needed"),
    "arp_acl_block": _c("arp_acl_block", "unreachable", "path_ping"),
    "arp_cache_poisoning": _c("arp_cache_poisoning", "unreachable", "path_ping"),
    "bgp_acl_block": _c("bgp_acl_block", "control_plane", "control_plane_bgp"),
    "ospf_acl_block": _c("ospf_acl_block", "control_plane", "control_plane_ospf"),
    "icmp_acl_block": _c("icmp_acl_block", "unreachable", "path_ping"),
    "http_acl_block": _c("http_acl_block", "unreachable", "path_http"),
    "dns_port_blocked": _c("dns_port_blocked", "unreachable", "path_http"),
    "bmv2_switch_down": _c("bmv2_switch_down", "unreachable", "path_http"),
    "flow_rule_loop": _c("flow_rule_loop", "loss", "custom"),
    "flow_rule_shadowing": _c("flow_rule_shadowing", "unreachable", "custom"),
    "host_static_blackhole": _c("host_static_blackhole", "unreachable", "path_ping"),
    "icmp_frag_needed_filter_misconfiguration": _c(
        "icmp_frag_needed_filter_misconfiguration", "unreachable", "custom"
    ),
    "k8s_networkpolicy_deny": _c(
        "k8s_networkpolicy_deny", "isolation", "isolation_http"
    ),
    "p4_table_entry_missing": _c("p4_table_entry_missing", "unreachable", "path_http"),
    "p4_table_entry_misconfig": _c(
        "p4_table_entry_misconfig", "unreachable", "path_http"
    ),
    "p4_action_selector_member_misconfig": _c(
        "p4_action_selector_member_misconfig", "loss", "custom"
    ),
    "p4_ecmp_group_member_missing": _c(
        "p4_ecmp_group_member_missing", "degradation", "custom"
    ),
    "p4runtime_pipeline_mismatch": _c(
        "p4runtime_pipeline_mismatch", "unreachable", "path_ping"
    ),
    "p4runtime_partial_write": _c(
        "p4runtime_partial_write", "unreachable", "path_http"
    ),
    "p4_table_resource_exhaustion": _c(
        "p4_table_resource_exhaustion", "control_plane", "custom"
    ),
    # The silent drop matches the destination address. Ping that address.
    # HTTP to a service DIP is not a healthy client path on the gateway fabric.
    "p4_tcam_entry_corruption": _c("p4_tcam_entry_corruption", "unreachable", "custom"),
    "int_insufficient_mtu_headroom": _c(
        "int_insufficient_mtu_headroom", "unreachable", "custom"
    ),
    "vrf_dscp_remarking": _c("vrf_dscp_remarking", "degradation", "custom"),
    "wireguard_allowed_ips_misconfiguration": _c(
        "wireguard_allowed_ips_misconfiguration", "unreachable", "path_ping"
    ),
    "wireguard_peer_key_misconfiguration": _c(
        "wireguard_peer_key_misconfiguration", "unreachable", "path_ping"
    ),
    "k8s_clusterip_routing_broken": _c(
        "k8s_clusterip_routing_broken", "unreachable", "custom"
    ),
    "load_balancer_overload": _c("load_balancer_overload", "degradation", "custom"),
    "lb_connection_state_exhaustion": _c(
        "lb_connection_state_exhaustion", "gray", "custom"
    ),
    # Unsafe pool update is evidenced by P4Runtime VIP/pool state (verify_fault);
    # VIP HTTP remains reachable, so path_http+gray_loss is the wrong probe.
    "lb_pending_connection_update_race": _c(
        "lb_pending_connection_update_race", "gray", "custom"
    ),
    "snat_port_pool_exhaustion": _c(
        "snat_port_pool_exhaustion", "unreachable", "custom"
    ),
    "nat_mapping_removed_without_drain": _c(
        "nat_mapping_removed_without_drain", "unreachable", "custom"
    ),
    "k8s_worker_apiserver_partition": _c(
        "k8s_worker_apiserver_partition", "control_plane", "custom"
    ),
    "sdn_controller_crash": _c("sdn_controller_crash", "control_plane", "custom"),
    "southbound_port_block": _c("southbound_port_block", "control_plane", "custom"),
    "southbound_port_mismatch": _c(
        "southbound_port_mismatch", "control_plane", "custom"
    ),
    "mac_address_conflict": _c("mac_address_conflict", "unreachable", "custom"),
    "dhcp_missing_subnet": _c("dhcp_missing_subnet", "unreachable", "custom"),
    "dhcp_service_down": _c("dhcp_service_down", "unreachable", "custom"),
    "dns_record_error": _c("dns_record_error", "unreachable", "dns_answer"),
    "dns_service_down": _c("dns_service_down", "unreachable", "path_http"),
    "host_incorrect_dns": _c("host_incorrect_dns", "unreachable", "http_by_name"),
    "host_incorrect_gateway": _c("host_incorrect_gateway", "unreachable", "path_ping"),
    "host_incorrect_ip": _c("host_incorrect_ip", "unreachable", "ping_old_ip"),
    "host_incorrect_netmask": _c(
        "host_incorrect_netmask", "degradation", "route_get_onlink"
    ),
    "host_ip_conflict": _c("host_ip_conflict", "unreachable", "custom"),
    "host_missing_ip": _c("host_missing_ip", "unreachable", "path_ping"),
    "k8s_coredns_isolated": _c("k8s_coredns_isolated", "isolation", "isolation_http"),
    "dns_lookup_latency": _c("dns_lookup_latency", "latency", "http_by_name"),
    "receiver_resource_contention": _c(
        "receiver_resource_contention", "degradation", "custom"
    ),
    "sender_resource_contention": _c(
        "sender_resource_contention", "degradation", "custom"
    ),
    "incast_traffic_network_limitation": _c(
        "incast_traffic_network_limitation", "degradation", "custom"
    ),
    "link_capacity_bottleneck": _c("link_capacity_bottleneck", "degradation", "custom"),
    "tcp_receive_window_limited": _c(
        "tcp_receive_window_limited", "degradation", "custom"
    ),
    "p4_ecn_threshold_misconfiguration": _c(
        "p4_ecn_threshold_misconfiguration", "gray", "custom"
    ),
    "bgp_hijacking": _c("bgp_hijacking", "control_plane", "bgp_hijack_route"),
    "dhcp_spoofed_dns": _c("dhcp_spoofed_dns", "unreachable", "custom"),
    "dhcp_spoofed_gateway": _c("dhcp_spoofed_gateway", "unreachable", "custom"),
    "dhcp_spoofed_subnet": _c("dhcp_spoofed_subnet", "unreachable", "custom"),
    "tcp_syn_flood_attack": _c("tcp_syn_flood_attack", "degradation", "custom"),
    "web_dos_attack": _c("web_dos_attack", "degradation", "custom"),
}


def get_symptom_contract(failure: str) -> SymptomContract:
    if failure not in _SYMPTOM_CONTRACTS:
        return SymptomContract(
            failure=failure,
            symptom_class="unreachable",
            probe="path_ping",
        )
    return _SYMPTOM_CONTRACTS[failure]


def list_symptom_contracts() -> list[SymptomContract]:
    return list(_SYMPTOM_CONTRACTS.values())
