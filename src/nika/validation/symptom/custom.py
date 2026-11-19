"""Custom evaluate_symptom handlers for failures with domain-specific gates."""

from __future__ import annotations

import time
from typing import Any

from nika.problems.base import build_verify_result
from nika.validation.symptom.link_latency_probes import latency_baseline, link_delayed
from nika.validation.symptom.route_leak_probes import (
    route_leak_baseline,
    route_leak_detour,
)
from nika.validation.symptom.bgp_surge_probes import (
    prefix_session_baseline,
    low_prefix_headroom,
    prefix_tables_grow,
)
from nika.validation.symptom.wireguard_probes import (
    peer_key_baseline,
    peer_key_unreachable,
)
from nika.validation.symptom.rib_fib_probes import rib_fib_baseline, rib_fib_diverged
from nika.validation.symptom.vrf_probes import (
    vrf_import_baseline,
    vrf_import_routes_lost,
)
from nika.validation.symptom.bgp_afi_probes import (
    ipv4_family_baseline,
    ipv4_family_missing,
)
from nika.validation.symptom.bgp_md5_probes import md5_baseline, md5_paths_withdrawn
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
    dhcp_option_dns,
    dhcp_option_gateway,
    dhcp_option_subnet,
    dhcp_unleased,
)
from nika.validation.symptom.dns_probes import (
    dns_baseline,
    dns_name_unresolvable,
    dns_wrong_record,
)
from nika.validation.symptom.bgp_prefix_probes import (
    prefix_length_baseline,
    prefix_length_drops_routes,
)
from nika.validation.symptom.bgp_route_probes import (
    advertisement_baseline,
    advertisement_withdrawn,
    hijack_attracts,
    hijack_baseline,
    rpki_leak_baseline,
    rpki_leak_split,
    rov_policy_baseline,
    rov_policy_missing,
    session_policy_baseline,
    session_policy_drops_routes,
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
    lb_race,
    lb_race_baseline,
    silent_loss,
    silent_loss_baseline,
    syn_flood,
    syn_flood_baseline,
)
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
from nika.validation.symptom.ospf_range_probes import (
    area_range_baseline,
    area_range_suppressed,
)
from nika.validation.symptom.routing_probes import (
    bgp_all_lost,
    bgp_bad_peer_as,
    bgp_baseline,
    bgp_some_lost,
    ospf_all_lost,
    ospf_baseline,
    ospf_full_unrouted,
    ospf_timer_mismatch,
    ospf_stub_area_mismatch,
    ospf_cut_edge_baseline,
    ospf_full_unrouted_baseline,
    ospf_one_lost,
    ospf_some_lost,
    ospf_stub_baseline,
    ospf_stubs_withdrawn,
    isis_adj_and_stubs_lost,
    isis_authentication_baseline,
    isis_authentication_lost,
    isis_stub_baseline,
    isis_stubs_withdrawn,
    routing_all_lost,
    routing_baseline,
)
from nika.validation.symptom.perf_probes import (
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
    switch_controller_conflict,
)
from nika.validation.symptom.ingress_probes import (
    ingress_rate_baseline,
    ingress_rate_limited,
)
from nika.validation.symptom.coredns_loop_probes import (
    forward_loop_baseline,
    forward_loop_broken,
)
from nika.validation.symptom.k8s_probes import (
    ingress_class_baseline,
    ingress_class_broken,
    clusterip_baseline,
    clusterip_broken,
    coredns_isolated,
    coredns_baseline,
    lb_advertisement_baseline,
    lb_advertisement_withdrawn,
    noexecute_taint_baseline,
    noexecute_taint_broken,
    readiness_baseline,
    readiness_broken,
    replicas_zero_baseline,
    replicas_zero_broken,
    selector_broken,
    target_port_baseline,
    target_port_broken,
    vxlan_neighbor_baseline,
    vxlan_neighbor_stale,
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
    flow_blackhole,
    flow_blackhole_baseline,
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

    Reads intervals whose logged end time follows the probe. The scenario's own
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
    fresh = fresh[-_RTP_INTERVALS:]
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
    intervals = current["intervals"][-_RTP_INTERVALS:]
    received_media = any(int(row["rx"]) > 0 for row in intervals)
    if not received_media:
        current["error"] = "no_received_media"
    sustained = (
        len(intervals) == _RTP_INTERVALS
        and len({row["time"] for row in intervals}) == _RTP_INTERVALS
        and all(
            int(row["rx"]) > 0
            and 100.0 * int(row["lost"]) / (int(row["rx"]) + int(row["lost"]))
            >= _RTP_LOSS_PERCENT
            for row in intervals
        )
    )
    if sham is None and isinstance(loss, float):
        problem._audit_rtp_sham = loss
    reference = base if sham is None else min(base, sham)
    verified = (
        sustained
        and isinstance(loss, float)
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


def _overlay_ef_sample(problem: Any, params: Any, media_probe) -> tuple[bool, dict]:
    from nika.net_env.verify import ping_stats
    from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import primary_ipv4

    before = {q: problem.queue_state(params, q) for q in ("ef", "be")}
    media_ok, media = media_probe(problem, params)
    ping = ping_stats(
        problem.runtime,
        params.src_host,
        primary_ipv4(problem.runtime, params.dst_host),
        count=5,
        interval_sec=0.2,
    )
    after = {q: problem.queue_state(params, q) for q in ("ef", "be")}
    delta = {
        q: {k: after[q][k] - before[q][k] for k in ("bytes", "packets", "drops")}
        for q in before
    }
    control_ok = (
        ping.loss_percent == 0
        and delta["be"]["bytes"] > 0
        and delta["be"]["drops"] == 0
    )
    return media_ok and control_ok, {
        "media": media,
        "before": before,
        "after": after,
        "delta": delta,
        "ping_loss_percent": ping.loss_percent,
        "ping_rtt_ms": ping.rtt_avg_ms,
        "control_ok": control_ok,
    }


def _overlay_ef_baseline(problem: Any, params: Any) -> tuple[bool, dict]:
    # Recovery can drain a standing FIFO during the first media reading.
    # Require a fresh window with healthy media and no further queue drops.
    attempts = []
    for _ in range(3):
        ok, sample = _overlay_ef_sample(problem, params, _vrf_dscp_baseline)
        ok = (
            ok
            and sample["delta"]["ef"]["packets"] > 0
            and sample["delta"]["ef"]["drops"] == 0
        )
        attempts.append(
            {
                "media_loss_percent": sample["media"]["loss_percent"],
                "ef_drops": sample["delta"]["ef"]["drops"],
                "control_ok": sample["control_ok"],
            }
        )
        if ok:
            break
    sample["attempts"] = attempts
    problem._audit_overlay_baseline = sample
    return ok, sample


def _overlay_ef_undersized(problem: Any, params: Any) -> tuple[bool, dict]:
    # At CHR rates the existing FIFO can hold over 30 seconds of media.
    # Receive sequence gaps appear after those already queued packets leave.
    attempts = []
    for _ in range(3):
        ok, sample = _overlay_ef_sample(problem, params, _vrf_dscp_remarking)
        attempts.append(sample)
        ok = (
            ok
            and sample["delta"]["ef"]["drops"] > 0
            and sample["delta"]["ef"]["packets"] > 0
        )
        if ok:
            break
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={
            "baseline": getattr(problem, "_audit_overlay_baseline", None),
            "attempts": attempts,
            **sample,
        },
    )


def host_acl_symptom(problem, params):
    from nika.validation.symptom.probe import run_probe_snapshot

    path = problem.probe_path(params)
    if path is None:
        return False, {"error": "no_probe_path"}
    probe = "path_http" if params.protocol == "http" else "path_ping"
    observed = run_probe_snapshot(problem.runtime, probe, path, params=params)
    failed = (
        observed.http_ok is False
        if params.protocol == "http"
        else observed.ping_ok is False
    )
    return failed, observed.as_dict()


def host_acl_baseline(problem, params):
    failed, observed = host_acl_symptom(problem, params)
    key = "http_ok" if params.protocol == "http" else "ping_ok"
    return observed.get(key) is True, observed


_CUSTOM: dict[str, Any] = {
    "host_acl_block": host_acl_symptom,
    "link_latency": link_delayed,
    "bgp_route_leak": route_leak_detour,
    "wireguard_peer_key_misconfiguration": peer_key_unreachable,
    "rib_fib_inconsistency": rib_fib_diverged,
    "overlay_qos_ef_undersized": _overlay_ef_undersized,
    "ospf_area_range_suppression": area_range_suppressed,
    "link_down": link_path_lost,
    "link_flap": evaluate_link_flap_symptom,
    "mtu_mismatch": mtu_reduced,
    "host_static_blackhole": blackhole_dropped,
    "host_missing_ip": assigned_address_unreachable,
    "host_incorrect_ip": assigned_address_unreachable,
    "host_incorrect_netmask": netmask_offsubnet_unreachable,
    "link_packet_corruption": evaluate_link_corruption_symptom,
    "link_capacity_bottleneck": evaluate_link_capacity_symptom,
    "device_forwarding_packet_corruption": evaluate_device_forwarding_corruption_symptom,
    "device_flow_blackhole": flow_blackhole,
    "web_dos_attack": web_dos,
    "endpoint_cpu_contention": sender_contention,
    "tcp_receive_window_limited": rwnd_limited,
    "vrf_dscp_remarking": _vrf_dscp_remarking,
    "southbound_port_block": controller_lost,
    "southbound_port_mismatch": controller_lost,
    "sdn_controller_crash": controller_lost,
    "switch_datapath_id_conflict": switch_controller_conflict,
    "flow_rule_loop": flow_loop,
    "k8s_node_firewall_drop": clusterip_broken,
    "k8s_vxlan_neighbor_stale": vxlan_neighbor_stale,
    "k8s_service_target_port_mismatch": target_port_broken,
    "k8s_service_selector_mismatch": selector_broken,
    "k8s_ingress_class_mismatch": ingress_class_broken,
    "k8s_ingress_rate_limit_misconfig": ingress_rate_limited,
    "lb_advertisement_missing": lb_advertisement_withdrawn,
    "k8s_coredns_forward_loop": forward_loop_broken,
    "k8s_worker_apiserver_partition": worker_partition,
    "k8s_readiness_probe_misconfig": readiness_broken,
    "k8s_deployment_replicas_zero": replicas_zero_broken,
    "k8s_node_noexecute_taint": noexecute_taint_broken,
    "silent_egress_packet_loss": silent_loss,
    "lb_connection_state_exhaustion": lb_race,
    "snat_port_pool_exhaustion": snat_pool_exhaustion,
    "nat_mapping_removed_without_drain": nat_mapping_removed,
    "mac_address_conflict": mac_conflict,
    "host_ip_conflict": ip_conflict,
    "icmp_frag_needed_filter_misconfiguration": frag_needed_filtered,
    "tcp_syn_flood_attack": syn_flood,
    "lb_pending_connection_update_race": lb_race,
    "flow_rule_shadowing": flow_shadow,
    "p4_ecn_marking_disabled": ecn_marking,
    "p4_ecmp_group_member_missing": ecmp_member_missing,
    "p4_action_selector_member_misconfig": selector_member_blackhole,
    "p4_table_resource_exhaustion": table_exhaustion,
    "p4runtime_partial_write": crossed_routes,
    "dhcp_option_gateway": dhcp_option_gateway,
    "dhcp_option_dns": dhcp_option_dns,
    "dhcp_option_subnet": dhcp_option_subnet,
    "bgp_acl_block": bgp_all_lost,
    "bgp_ttl_security_mismatch": bgp_some_lost,
    "bgp_md5_password_mismatch": md5_paths_withdrawn,
    "bgp_session_policy_missing": session_policy_drops_routes,
    "vrf_route_target_mismatch": vrf_import_routes_lost,
    "bgp_prefix_list_length_mismatch": prefix_length_drops_routes,
    "bgp_address_family_not_activated": ipv4_family_missing,
    "bgp_asn_misconfig": bgp_bad_peer_as,
    "routing_service_down": routing_all_lost,
    "bgp_max_prefix_limit_low": low_prefix_headroom,
    "bgp_prefix_surge": prefix_tables_grow,
    "ospf_acl_block": ospf_all_lost,
    "ospf_area_misconfiguration": ospf_some_lost,
    "ospf_interface_not_enabled": ospf_one_lost,
    "ospf_duplicate_router_id": ospf_stubs_withdrawn,
    "ospf_network_type_mismatch": ospf_full_unrouted,
    "ospf_hello_interval_mismatch": ospf_timer_mismatch,
    "ospf_stub_area_mismatch": ospf_stub_area_mismatch,
    "isis_metric_style_mismatch": isis_stubs_withdrawn,
    "isis_router_level_mismatch": isis_adj_and_stubs_lost,
    "isis_authentication_mismatch": isis_authentication_lost,
    "bgp_hijacking": lambda p, q: (
        rpki_leak_split(p, q)
        if q.variant == "rpki_invalid_origin"
        else hijack_attracts(p, q)
    ),
    "bgp_rov_not_enforced": rov_policy_missing,
    "bgp_missing_route_advertisement": advertisement_withdrawn,
    "dhcp_missing_subnet": dhcp_unleased,
    "dhcp_service_down": dhcp_unleased,
    "dns_port_blocked": lambda p, q: (
        coredns_isolated(p, q)
        if getattr(p.net_env, "kubernetes_nodes", None)
        else dns_name_unresolvable(p, q)
    ),
    "dns_service_down": dns_name_unresolvable,
    "dns_record_error": dns_wrong_record,
}

# Pre-inject measurements of the same signal a custom probe reads after inject.
_CUSTOM_BASELINE: dict[str, Any] = {
    "host_acl_block": host_acl_baseline,
    "link_latency": latency_baseline,
    "bgp_route_leak": route_leak_baseline,
    "wireguard_peer_key_misconfiguration": peer_key_baseline,
    "rib_fib_inconsistency": rib_fib_baseline,
    "overlay_qos_ef_undersized": _overlay_ef_baseline,
    "ospf_area_range_suppression": area_range_baseline,
    "web_dos_attack": web_dos_baseline,
    "endpoint_cpu_contention": sender_baseline,
    "tcp_receive_window_limited": rwnd_baseline,
    "southbound_port_block": controller_serving,
    "southbound_port_mismatch": controller_serving,
    "sdn_controller_crash": controller_serving,
    "switch_datapath_id_conflict": controller_serving,
    "link_down": link_path_baseline,
    "link_flap": flap_baseline,
    "link_packet_corruption": link_corruption_baseline,
    "device_forwarding_packet_corruption": forwarding_corruption_baseline,
    "device_flow_blackhole": flow_blackhole_baseline,
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
    "tcp_syn_flood_attack": syn_flood_baseline,
    "lb_pending_connection_update_race": lb_race_baseline,
    "lb_connection_state_exhaustion": lb_race_baseline,
    "flow_rule_shadowing": flow_shadow_baseline,
    "flow_rule_loop": flow_loop_baseline,
    "p4_ecn_marking_disabled": ecn_marking_baseline,
    "p4_ecmp_group_member_missing": ecmp_sweep_baseline,
    "p4_action_selector_member_misconfig": selector_sweep_baseline,
    "p4_table_resource_exhaustion": table_exhaustion_baseline,
    "p4runtime_partial_write": crossed_routes_baseline,
    "silent_egress_packet_loss": silent_loss_baseline,
    "k8s_worker_apiserver_partition": worker_partition_baseline,
    "k8s_readiness_probe_misconfig": readiness_baseline,
    "k8s_deployment_replicas_zero": replicas_zero_baseline,
    "k8s_node_noexecute_taint": noexecute_taint_baseline,
    "k8s_node_firewall_drop": clusterip_baseline,
    "k8s_vxlan_neighbor_stale": vxlan_neighbor_baseline,
    "k8s_service_target_port_mismatch": target_port_baseline,
    "k8s_service_selector_mismatch": target_port_baseline,
    "k8s_ingress_class_mismatch": ingress_class_baseline,
    "k8s_ingress_rate_limit_misconfig": ingress_rate_baseline,
    "lb_advertisement_missing": lb_advertisement_baseline,
    "k8s_coredns_forward_loop": forward_loop_baseline,
    "dhcp_option_gateway": dhcp_client_baseline,
    "dhcp_option_dns": dhcp_client_baseline,
    "dhcp_option_subnet": dhcp_client_baseline,
    "dhcp_missing_subnet": dhcp_offer_baseline,
    "dhcp_service_down": dhcp_offer_baseline,
    "bgp_acl_block": bgp_baseline,
    "bgp_ttl_security_mismatch": bgp_baseline,
    "bgp_md5_password_mismatch": md5_baseline,
    "bgp_session_policy_missing": session_policy_baseline,
    "vrf_route_target_mismatch": vrf_import_baseline,
    "bgp_prefix_list_length_mismatch": prefix_length_baseline,
    "bgp_address_family_not_activated": ipv4_family_baseline,
    "bgp_asn_misconfig": bgp_baseline,
    "routing_service_down": routing_baseline,
    "bgp_max_prefix_limit_low": prefix_session_baseline,
    "bgp_prefix_surge": prefix_session_baseline,
    "ospf_acl_block": ospf_baseline,
    "ospf_area_misconfiguration": ospf_baseline,
    "ospf_interface_not_enabled": ospf_baseline,
    "ospf_duplicate_router_id": ospf_stub_baseline,
    "ospf_network_type_mismatch": ospf_full_unrouted_baseline,
    "ospf_hello_interval_mismatch": ospf_cut_edge_baseline,
    "ospf_stub_area_mismatch": ospf_cut_edge_baseline,
    "isis_metric_style_mismatch": isis_stub_baseline,
    "isis_router_level_mismatch": isis_stub_baseline,
    "isis_authentication_mismatch": isis_authentication_baseline,
    "bgp_hijacking": lambda p, q: (
        rpki_leak_baseline(p, q)
        if q.variant == "rpki_invalid_origin"
        else hijack_baseline(p, q)
    ),
    "bgp_rov_not_enforced": rov_policy_baseline,
    "bgp_missing_route_advertisement": advertisement_baseline,
    "vrf_dscp_remarking": _vrf_dscp_baseline,
    "dns_port_blocked": lambda p, q: (
        coredns_baseline(p, q)
        if getattr(p.net_env, "kubernetes_nodes", None)
        else dns_baseline(p, q)
    ),
    "dns_service_down": dns_baseline,
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
