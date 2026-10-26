"""Custom evaluate_symptom handlers for failures with domain-specific gates."""

from __future__ import annotations

import time
from typing import Any

from nika.problems.base import build_verify_result
from nika.validation.symptom.addressing_probes import (
    assigned_address_baseline,
    assigned_address_unreachable,
    ip_conflict,
    ip_conflict_baseline,
    mac_conflict,
    mac_conflict_baseline,
    netmask_baseline,
    netmask_offsubnet_unreachable,
)
from nika.validation.symptom.dhcp_probes import (
    dhcp_client_baseline,
    dhcp_offer_baseline,
    dhcp_spoofed_dns,
    dhcp_spoofed_gateway,
    dhcp_spoofed_subnet,
    dhcp_unleased,
)
from nika.validation.symptom.dns_probes import (
    dns_baseline,
    dns_lookup_slow,
    dns_name_unresolvable,
    dns_wrong_record,
)
from nika.validation.symptom.bgp_route_probes import (
    advertisement_baseline,
    advertisement_withdrawn,
    hijack_attracts,
    hijack_baseline,
    rpki_leak_baseline,
    rpki_leak_split,
)
from nika.validation.symptom.flap_probes import (
    evaluate_link_flap_symptom,
    flap_baseline,
)
from nika.validation.symptom.icmp_probes import (
    frag_needed_baseline,
    frag_needed_filtered,
)
from nika.validation.symptom.p4_gateway_probes import (
    ecn_marking,
    ecn_marking_baseline,
    int_headroom,
    int_headroom_baseline,
    lb_race,
    lb_race_baseline,
    silent_loss,
    silent_loss_baseline,
    syn_flood,
    syn_flood_baseline,
    tcam_drop,
    tcam_drop_baseline,
)
from nika.validation.symptom.queueing_probes import incast_baseline, incast_queueing
from nika.validation.symptom.p4_runtime_probes import (
    crossed_routes,
    crossed_routes_baseline,
    ecmp_member_missing,
    ecmp_sweep_baseline,
    selector_member_blackhole,
    selector_sweep_baseline,
    table_exhaustion,
    table_exhaustion_baseline,
)
from nika.validation.symptom.routing_probes import (
    bgp_all_lost,
    bgp_bad_peer_as,
    bgp_baseline,
    max_prefix_baseline,
    max_prefix_cease,
    ospf_all_lost,
    ospf_baseline,
    ospf_one_lost,
    ospf_some_lost,
    routing_all_lost,
    routing_baseline,
)
from nika.validation.symptom.perf_probes import (
    lb_baseline,
    lb_overload,
    receiver_baseline,
    receiver_contention,
    rwnd_baseline,
    rwnd_limited,
    sender_baseline,
    sender_contention,
    web_dos,
    web_dos_baseline,
)
from nika.validation.symptom.sdn_probes import (
    controller_lost,
    controller_serving,
    flow_loop,
    flow_loop_baseline,
    flow_shadow,
    flow_shadow_baseline,
)
from nika.validation.symptom.k8s_probes import (
    clusterip_baseline,
    clusterip_broken,
    coredns_baseline,
    coredns_isolated,
    worker_partition,
    worker_partition_baseline,
)
from nika.validation.symptom.nat_probes import (
    nat_flow_baseline,
    nat_mapping_removed,
    snat_pool_baseline,
    snat_pool_exhaustion,
)
from nika.validation.symptom.corruption_probes import (
    capacity_baseline,
    evaluate_device_forwarding_corruption_symptom,
    evaluate_link_capacity_symptom,
    evaluate_link_corruption_symptom,
    forwarding_corruption_baseline,
    link_corruption_baseline,
)
from nika.validation.symptom.path_probes import (
    blackhole_baseline,
    blackhole_dropped,
    link_path_baseline,
    link_path_lost,
    mtu_baseline,
    mtu_reduced,
)


# Receive loss of the realtime call on the remark target's path must reach
# this level (and rise this far over the healthy reading) to count as degraded.
_RTP_LOSS_PERCENT = 10.0
_RTP_LOSS_DELTA = 8.0
_RTP_INTERVALS = 3
_RTP_BASELINE_ATTEMPTS = 3
_RTP_HEALTHY_LOSS_PERCENT = 5.0
_RTP_WAIT_SEC = 50.0


def _rtp_quality(problem: Any, params: Any) -> dict[str, Any]:
    """Call statistics the receiving softphone logs for the sender's stream.

    Reads only intervals that start after the probe does. The scenario's own
    call carries the traffic, so the probe adds no load of its own.
    """
    from nika.net_env.enterprise_branch.workload import MEDIA_LOG
    from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import primary_ipv4
    from nika.traffic.apps.media_peer import parse_stats

    runtime = problem.runtime
    src_ip = primary_ipv4(runtime, params.src_host)
    start = runtime.exec(params.dst_host, "date -u +%Y-%m-%dT%H:%M:%SZ").strip()
    deadline = time.monotonic() + _RTP_WAIT_SEC
    fresh: list[dict[str, Any]] = []
    while True:
        out = runtime.exec(params.dst_host, f"tail -n 200 {MEDIA_LOG} 2>/dev/null")
        rows = [parse_stats(line) for line in out.splitlines()]
        fresh = [
            r for r in rows if r and r["source"] == src_ip and str(r["time"]) > start
        ]
        if len(fresh) >= _RTP_INTERVALS or time.monotonic() > deadline:
            break
        time.sleep(5.0)
    rx = sum(int(r["rx"]) for r in fresh)
    lost = sum(int(r["lost"]) for r in fresh)
    return {
        "src_ip": src_ip,
        "intervals": fresh,
        "loss_percent": 100.0 * lost / (rx + lost) if rx + lost else None,
        "max_jitter_ms": max((float(r["jitter"]) for r in fresh), default=None),
        "error": None if fresh else "no_call_statistics",
    }


def _vrf_dscp_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Healthy receive loss of the call on the remark target's path.

    Host saturation can spike one reading, so it is re-measured while the
    pooled loss stays above ``_RTP_HEALTHY_LOSS_PERCENT``; a reading that
    never settles marks the baseline unreliable.
    """
    attempts: list[dict[str, Any]] = []
    for _ in range(_RTP_BASELINE_ATTEMPTS):
        sample = _rtp_quality(problem, params)
        attempts.append(sample)
        loss = sample["loss_percent"]
        if isinstance(loss, float) and loss <= _RTP_HEALTHY_LOSS_PERCENT:
            break
    loss = attempts[-1]["loss_percent"]
    reliable = isinstance(loss, float) and loss <= _RTP_HEALTHY_LOSS_PERCENT
    problem._audit_rtp_baseline = loss
    problem._audit_rtp_sham = None
    return reliable, {
        **attempts[-1],
        "baseline_reliable": reliable,
        "attempt_loss_percent": [a["loss_percent"] for a in attempts],
        **({} if reliable else {"error": "healthy_baseline_unreliable"}),
    }


def _vrf_dscp_remarking(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Demoted media competes with bulk in best effort and loses packets.

    The healthy reference is the lower of the baseline and the sham reading
    (the first read after the baseline, before inject), so one noisy healthy
    measurement cannot hide the delta.
    """
    base = getattr(problem, "_audit_rtp_baseline", None)
    if not isinstance(base, float):
        return False, {"error": "no_healthy_rtp_baseline"}
    sham = getattr(problem, "_audit_rtp_sham", None)
    current = _rtp_quality(problem, params)
    loss = current["loss_percent"]
    if sham is None and isinstance(loss, float):
        problem._audit_rtp_sham = loss
    reference = base if sham is None else min(base, sham)
    verified = (
        isinstance(loss, float)
        and loss >= _RTP_LOSS_PERCENT
        and loss - reference >= _RTP_LOSS_DELTA
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "baseline_loss_percent": base,
            "sham_loss_percent": sham,
            "reference_loss_percent": reference,
            "current": current,
        },
    )


_CUSTOM: dict[str, Any] = {
    "incast_traffic_network_limitation": incast_queueing,
    "link_down": link_path_lost,
    "link_detach": link_path_lost,
    "link_flap": evaluate_link_flap_symptom,
    "mtu_mismatch": mtu_reduced,
    "host_static_blackhole": blackhole_dropped,
    "host_missing_ip": assigned_address_unreachable,
    "host_incorrect_ip": assigned_address_unreachable,
    "host_incorrect_netmask": netmask_offsubnet_unreachable,
    "link_packet_corruption": evaluate_link_corruption_symptom,
    "link_capacity_bottleneck": evaluate_link_capacity_symptom,
    "device_forwarding_packet_corruption": evaluate_device_forwarding_corruption_symptom,
    "web_dos_attack": web_dos,
    "sender_resource_contention": sender_contention,
    "receiver_resource_contention": receiver_contention,
    "tcp_receive_window_limited": rwnd_limited,
    "vrf_dscp_remarking": _vrf_dscp_remarking,
    "southbound_port_block": controller_lost,
    "southbound_port_mismatch": controller_lost,
    "sdn_controller_crash": controller_lost,
    "flow_rule_loop": flow_loop,
    "k8s_clusterip_routing_broken": clusterip_broken,
    "k8s_coredns_isolated": coredns_isolated,
    "k8s_worker_apiserver_partition": worker_partition,
    "silent_egress_packet_loss": silent_loss,
    "load_balancer_overload": lb_overload,
    "lb_connection_state_exhaustion": lb_race,
    "snat_port_pool_exhaustion": snat_pool_exhaustion,
    "nat_mapping_removed_without_drain": nat_mapping_removed,
    "mac_address_conflict": mac_conflict,
    "host_ip_conflict": ip_conflict,
    "icmp_frag_needed_filter_misconfiguration": frag_needed_filtered,
    "int_insufficient_mtu_headroom": int_headroom,
    "tcp_syn_flood_attack": syn_flood,
    "lb_pending_connection_update_race": lb_race,
    "flow_rule_shadowing": flow_shadow,
    "p4_ecn_threshold_misconfiguration": ecn_marking,
    "p4_tcam_entry_corruption": tcam_drop,
    "p4_ecmp_group_member_missing": ecmp_member_missing,
    "p4_action_selector_member_misconfig": selector_member_blackhole,
    "p4_table_resource_exhaustion": table_exhaustion,
    "p4runtime_partial_write": crossed_routes,
    "dhcp_spoofed_gateway": dhcp_spoofed_gateway,
    "dhcp_spoofed_dns": dhcp_spoofed_dns,
    "dhcp_spoofed_subnet": dhcp_spoofed_subnet,
    "bgp_acl_block": bgp_all_lost,
    "bgp_asn_misconfig": bgp_bad_peer_as,
    "bgp_max_prefix_exceeded": max_prefix_cease,
    "frr_service_down": routing_all_lost,
    "ospf_acl_block": ospf_all_lost,
    "ospf_area_misconfiguration": ospf_some_lost,
    "ospf_neighbor_missing": ospf_one_lost,
    "bgp_hijacking": hijack_attracts,
    "bgp_rpki_invalid_route_leak": rpki_leak_split,
    "bgp_missing_route_advertisement": advertisement_withdrawn,
    "dhcp_missing_subnet": dhcp_unleased,
    "dhcp_service_down": dhcp_unleased,
    "dns_port_blocked": dns_name_unresolvable,
    "dns_service_down": dns_name_unresolvable,
    "dns_lookup_latency": dns_lookup_slow,
    "dns_record_error": dns_wrong_record,
}

# Pre-inject measurements of the same signal a custom probe reads after inject.
_CUSTOM_BASELINE: dict[str, Any] = {
    "receiver_resource_contention": receiver_baseline,
    "web_dos_attack": web_dos_baseline,
    "load_balancer_overload": lb_baseline,
    "sender_resource_contention": sender_baseline,
    "tcp_receive_window_limited": rwnd_baseline,
    "southbound_port_block": controller_serving,
    "southbound_port_mismatch": controller_serving,
    "sdn_controller_crash": controller_serving,
    "link_down": link_path_baseline,
    "link_detach": link_path_baseline,
    "link_flap": flap_baseline,
    "link_packet_corruption": link_corruption_baseline,
    "device_forwarding_packet_corruption": forwarding_corruption_baseline,
    "link_capacity_bottleneck": capacity_baseline,
    "mtu_mismatch": mtu_baseline,
    "host_static_blackhole": blackhole_baseline,
    "host_missing_ip": assigned_address_baseline,
    "host_incorrect_ip": assigned_address_baseline,
    "host_incorrect_netmask": netmask_baseline,
    "snat_port_pool_exhaustion": snat_pool_baseline,
    "nat_mapping_removed_without_drain": nat_flow_baseline,
    "mac_address_conflict": mac_conflict_baseline,
    "host_ip_conflict": ip_conflict_baseline,
    "icmp_frag_needed_filter_misconfiguration": frag_needed_baseline,
    "int_insufficient_mtu_headroom": int_headroom_baseline,
    "tcp_syn_flood_attack": syn_flood_baseline,
    "lb_pending_connection_update_race": lb_race_baseline,
    "lb_connection_state_exhaustion": lb_race_baseline,
    "flow_rule_shadowing": flow_shadow_baseline,
    "flow_rule_loop": flow_loop_baseline,
    "p4_ecn_threshold_misconfiguration": ecn_marking_baseline,
    "p4_tcam_entry_corruption": tcam_drop_baseline,
    "p4_ecmp_group_member_missing": ecmp_sweep_baseline,
    "p4_action_selector_member_misconfig": selector_sweep_baseline,
    "p4_table_resource_exhaustion": table_exhaustion_baseline,
    "p4runtime_partial_write": crossed_routes_baseline,
    "silent_egress_packet_loss": silent_loss_baseline,
    "incast_traffic_network_limitation": incast_baseline,
    "k8s_worker_apiserver_partition": worker_partition_baseline,
    "k8s_clusterip_routing_broken": clusterip_baseline,
    "k8s_coredns_isolated": coredns_baseline,
    "dhcp_spoofed_gateway": dhcp_client_baseline,
    "dhcp_spoofed_dns": dhcp_client_baseline,
    "dhcp_spoofed_subnet": dhcp_client_baseline,
    "dhcp_missing_subnet": dhcp_offer_baseline,
    "dhcp_service_down": dhcp_offer_baseline,
    "bgp_acl_block": bgp_baseline,
    "bgp_asn_misconfig": bgp_baseline,
    "bgp_max_prefix_exceeded": max_prefix_baseline,
    "frr_service_down": routing_baseline,
    "ospf_acl_block": ospf_baseline,
    "ospf_area_misconfiguration": ospf_baseline,
    "ospf_neighbor_missing": ospf_baseline,
    "bgp_hijacking": hijack_baseline,
    "bgp_rpki_invalid_route_leak": rpki_leak_baseline,
    "bgp_missing_route_advertisement": advertisement_baseline,
    "vrf_dscp_remarking": _vrf_dscp_baseline,
    "dns_port_blocked": dns_baseline,
    "dns_service_down": dns_baseline,
    "dns_lookup_latency": dns_baseline,
    "dns_record_error": dns_baseline,
}


def evaluate_custom_baseline(
    failure: str, problem: Any, params: Any
) -> tuple[bool, dict[str, Any]] | None:
    """Return the healthy pre-inject reading, or ``None`` without a baseline."""
    handler = _CUSTOM_BASELINE.get(failure)
    if handler is None:
        return None
    return handler(problem, params)


def evaluate_custom_symptom(
    failure: str, problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    handler = _CUSTOM.get(failure)
    if handler is None:
        return False, {"error": f"no_custom_handler:{failure}"}
    return handler(problem, params)
