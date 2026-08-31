"""Probe runners for test-path evaluate_symptom."""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from pydantic import BaseModel

from nika.net_env.verify import (
    exec_or_empty,
    frr_bgp_has_established_session,
    http_body_time_ms,
    http_download_stats,
    http_ok,
    http_time_ms,
    iperf_throughput_bps,
    median_float,
    ping_mtu_blackhole,
    ping_mtu_frag_needed,
    ping_ok,
    ping_stats,
    route_is_onlink,
    tbf_overlimits,
)
from nika.problems.support.probe_paths import ProbePath, get_probe_path
from tests.support.symptom.gray_probes import probe_gray_packet_loss
from tests.support.symptom.types import (
    ProbeKind,
    ProbeSnapshot,
    SymptomClass,
)
from nika.runtime.base import LabRuntime

# Scenario endpoint cross-subnet peers for host-local faults.
_CROSS_SUBNET_PEER: dict[str, dict[str, str]] = {
    "simple_bgp": {"pc1": "200.1.1.2", "pc2": "195.11.14.2"},
    "dc_clos": {
        "client_0": "10.0.1.2",
    },
}

_SCENARIO_PEER_HOST: dict[str, dict[str, str]] = {
    "simple_bgp": {"pc1": "pc2", "pc2": "pc1"},
    "dc_clos": {
        "client_0": "webserver0_pod0",
    },
    "campus_lan": {
        "pc_1_1_1_1": "pc_2_1_1_1",
        "pc_2_1_1_1": "pc_1_1_1_1",
    },
    "enterprise_branch": {
        "br1_corp_pc": "hq_corp_pc",
        "br2_corp_pc": "hq_corp_pc",
        "hq_corp_pc": "br1_corp_pc",
        "br1_guest_pc": "br1_edge",
        "br2_guest_pc": "br2_edge",
        "br1_iot_pc": "br1_edge",
        "br2_iot_pc": "br2_edge",
    },
    "sdn_l3_clos": {
        "client_1_1": "client_2_1",
        "client_2_1": "client_1_1",
    },
    "p4_dc_fabric": {
        "client_1_1": "client_2_1",
        "client_2_1": "client_1_1",
    },
    "p4_dc_gateway": {
        "client_1": "client_2",
        "client_2": "client_1",
    },
    "min3clos": {
        "client_1_1": "client_2_1",
        "client_2_1": "client_1_1",
    },
    "k8s_lab": {
        "client": "as2r1",
    },
    "llmd_lab": {
        "client": "web",
    },
}

_SCENARIO_NAME_URL: dict[str, str] = {
    "dc_clos": "http://web0.pod0/",
    "campus_lan": "http://web0.local/",
}


def _is_endpoint_host(name: str) -> bool:
    """Hosts where ACL/ARP/ICMP faults are injected on the endpoint itself."""
    return (
        name.startswith(("pc", "client", "host"))
        or "_corp_pc" in name
        or "_guest_pc" in name
        or "_iot_pc" in name
        or name.endswith(("_srv", "_srv2"))
    )


def _params_get(params: Any, key: str, default: str | None = None) -> str | None:
    if params is None:
        return default
    if isinstance(params, BaseModel):
        return getattr(params, key, default)
    if isinstance(params, dict):
        value = params.get(key, default)
        return str(value) if value is not None else default
    value = getattr(params, key, default)
    return str(value) if value is not None else default


def _resolve_path(
    scenario: str | None,
    params: Any,
    *,
    topo_size: str = "s",
) -> ProbePath | None:
    path = get_probe_path(scenario or "", topo_size=topo_size)
    if path is None:
        return None
    inject_host = _params_get(params, "host_name")
    explicit_observer = _params_get(params, "observer_device")
    src_host = explicit_observer or _params_get(params, "symptom_host") or path.src_host
    if not explicit_observer and inject_host and _is_endpoint_host(inject_host):
        src_host = inject_host
    dst_ip = _params_get(params, "probe_dst_ip") or path.dst_ip
    peer_host = _params_get(params, "peer_host") or path.peer_host
    peer_map = _CROSS_SUBNET_PEER.get(scenario or "", {})
    if inject_host and inject_host in peer_map:
        dst_ip = peer_map[inject_host]
        # Host-local path probes must source from the inject host.
        src_host = inject_host
    peer_hosts = _SCENARIO_PEER_HOST.get(scenario or "", {})
    if inject_host and inject_host in peer_hosts:
        peer_host = peer_hosts[inject_host]
    elif inject_host and inject_host.startswith(("pc", "client", "host", "br")):
        # Prefer scenario default peer when inject host is not the mapped source.
        if path.peer_host and path.peer_host != inject_host:
            peer_host = path.peer_host
        else:
            for mapped_peer in peer_hosts.values():
                if mapped_peer != inject_host:
                    peer_host = mapped_peer
                    break
    name_url = (
        _params_get(params, "http_name_url")
        or path.http_name_url
        or _SCENARIO_NAME_URL.get(scenario or "")
        or path.http_url
    )
    # Blackhole / incorrect_ip may pass explicit destinations.
    override_dst = _params_get(params, "probe_dst_ip")
    blackhole = _params_get(params, "blackhole_network")
    if override_dst:
        dst_ip = override_dst
    elif blackhole and "/" in blackhole:
        base = blackhole.split("/")[0].rsplit(".", 1)[0]
        dst_ip = f"{base}.2"
    old_ip = _params_get(params, "original_ip") or path.old_ip
    http_url = _params_get(params, "probe_url") or path.http_url
    # Silent destination drop (p4_tcam_entry_corruption): probe the corrupted
    # target, not the scenario default VIP which remains reachable.
    target_ip = _params_get(params, "target_ip")
    if target_ip:
        dst_ip = target_ip
        http_url = f"http://{target_ip}/"
    control_source = _params_get(params, "control_source")
    if (
        control_source
        and not explicit_observer
        and not _params_get(params, "symptom_host")
    ):
        src_host = control_source
    return ProbePath(
        src_host=src_host,
        dst_ip=dst_ip,
        http_url=http_url,
        symptom_url=_params_get(params, "symptom_url") or path.symptom_url,
        control_url=_params_get(params, "control_url") or path.control_url,
        control_plane_host=(
            _params_get(params, "host_name")
            or _params_get(params, "receiver_name")
            or path.control_plane_host
        ),
        ping_count=path.ping_count,
        gray_ping_count=path.gray_ping_count,
        http_name_url=name_url,
        peer_host=peer_host,
        old_ip=old_ip,
    )


def _resolve_blackhole_path(
    runtime: LabRuntime,
    params: Any,
    base: ProbePath,
) -> ProbePath:
    """Source from a leaf-local host toward the installed blackhole prefix."""
    router = _params_get(params, "host_name")
    if not router:
        return base
    stored = exec_or_empty(
        runtime, router, "cat /tmp/nika_blackhole_network 2>/dev/null || true"
    ).strip()
    src = exec_or_empty(
        runtime, router, "cat /tmp/nika_blackhole_src 2>/dev/null || true"
    ).strip()
    dst_ip = (
        exec_or_empty(
            runtime, router, "cat /tmp/nika_blackhole_dst 2>/dev/null || true"
        ).strip()
        or base.dst_ip
    )
    if not dst_ip and stored and "/" in stored:
        try:
            network = ipaddress.ip_network(stored, strict=False)
            hosts_in_net = list(network.hosts())
            dst_ip = str(
                hosts_in_net[-1] if hosts_in_net else network.network_address + 1
            )
        except ValueError:
            net = stored.split("/", 1)[0]
            base_octets = net.rsplit(".", 1)[0]
            dst_ip = f"{base_octets}.2"
    if not src:
        connected = runtime.get_connected_devices(router) or []
        for candidate in connected:
            if candidate != router and not candidate.endswith("_edge"):
                src = candidate
                break
    if not src:
        src = base.src_host
    return ProbePath(
        src_host=src,
        dst_ip=dst_ip,
        http_url=base.http_url,
        symptom_url=base.symptom_url,
        control_url=base.control_url,
        control_plane_host=base.control_plane_host,
        ping_count=base.ping_count,
        gray_ping_count=base.gray_ping_count,
        http_name_url=base.http_name_url,
        peer_host=base.peer_host,
        old_ip=base.old_ip,
    )


def _onlink_probe_dst(
    runtime: LabRuntime, host: str, fallback_dst: str | None
) -> str | None:
    """Pick an address inside the (wrong) attached prefix but outside a /24."""
    line = exec_or_empty(
        runtime, host, "ip -4 -o addr show dev eth0 scope global 2>/dev/null || true"
    )
    match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
    if not match:
        return fallback_dst
    ip_s, prefix_s = match.group(1), int(match.group(2))
    try:
        iface = ipaddress.ip_interface(f"{ip_s}/{prefix_s}")
    except ValueError:
        return fallback_dst
    if prefix_s >= 24:
        return fallback_dst
    # Prefer x.y.255.1 when still inside the wide prefix (classic /8 widening).
    parts = ip_s.split(".")
    candidate = f"{parts[0]}.{parts[1]}.255.1"
    try:
        if ipaddress.ip_address(candidate) in iface.network:
            return candidate
    except ValueError:
        pass
    # Fallback: network address + 1 inside the wide prefix.
    hosts = list(iface.network.hosts())
    if not hosts:
        return fallback_dst
    return str(hosts[min(10, len(hosts) - 1)])


_CLUSTER_DNS_NAME = "kubernetes.default.svc.cluster.local"


def _cluster_dns_ok(
    runtime: LabRuntime,
    host: str,
    name: str = _CLUSTER_DNS_NAME,
    *,
    server: str | None = None,
) -> bool:
    """True when DNS resolves ``name`` (k3s busybox nslookup)."""
    server_arg = f" {server}" if server else ""
    output = exec_or_empty(
        runtime,
        host,
        f"nslookup {name}{server_arg} 2>/dev/null || "
        f"busybox nslookup {name}{server_arg} 2>/dev/null || true",
        timeout=12,
    )
    lowered = output.lower()
    if any(
        token in lowered
        for token in (
            "can't find",
            "can't resolve",
            "nxdomain",
            "no servers could be reached",
            "timed out",
            "connection timed out",
        )
    ):
        return False
    return "name:" in lowered


def run_probe_snapshot(
    runtime: LabRuntime,
    probe_kind: ProbeKind,
    path: ProbePath,
    *,
    params: Any = None,
) -> ProbeSnapshot:
    snap = ProbeSnapshot()
    src = path.src_host
    if probe_kind == "gray_ping_loss" and path.dst_ip:
        ok, details = probe_gray_packet_loss(runtime, path)
        snap = ProbeSnapshot(
            ping_ok=details.get("received", 0) > 0,
            loss_percent=details.get("loss_percent"),
            extra=details,
        )
        if not ok:
            snap.extra["gray_probe_failed"] = True
        return snap
    if probe_kind in {"path_ping", "path_ping_loss"} and path.dst_ip:
        if probe_kind == "path_ping_loss":
            stats = ping_stats(runtime, src, path.dst_ip, count=path.ping_count)
        else:
            snap.ping_ok = ping_ok(runtime, src, path.dst_ip)
            return snap
        snap.ping_ok = stats.received > 0
        snap.loss_percent = stats.loss_percent
        snap.rtt_avg_ms = stats.rtt_avg_ms
        snap.rtt_mdev_ms = stats.rtt_mdev_ms
        return snap
    if probe_kind == "path_mtu_blackhole" and path.dst_ip:
        snap.mtu_blackhole = ping_mtu_blackhole(runtime, src, path.dst_ip)
        snap.ping_ok = ping_ok(runtime, src, path.dst_ip)
        return snap
    if probe_kind == "path_mtu_frag_needed" and path.dst_ip:
        snap.mtu_frag_needed = ping_mtu_frag_needed(runtime, src, path.dst_ip)
        snap.ping_ok = ping_ok(runtime, src, path.dst_ip)
        return snap
    if probe_kind in {"path_http", "degradation_http"} and path.http_url:
        if probe_kind == "path_http":
            snap.http_ok = http_ok(runtime, src, path.http_url)
            snap.http_time_ms = http_time_ms(runtime, src, path.http_url)
            return snap
        samples_raw = _params_get(params, "probe_samples", "1") or "1"
        timeout_raw = _params_get(params, "probe_timeout_sec", "20") or "20"
        try:
            sample_count = max(1, int(samples_raw))
        except ValueError:
            sample_count = 1
        try:
            timeout_sec = max(1, int(timeout_raw))
        except ValueError:
            timeout_sec = 20
        times_ms: list[float] = []
        codes: list[str] = []
        for _ in range(sample_count):
            result = http_download_stats(
                runtime,
                src,
                path.http_url,
                max_time_sec=timeout_sec,
                connect_timeout_sec=min(5, timeout_sec),
            )
            codes.append(result.http_code)
            if result.ok and result.time_total_s is not None:
                times_ms.append(result.time_total_s * 1000.0)
        success_rate = len(times_ms) / sample_count
        snap.http_ok = success_rate >= 0.8
        snap.http_time_ms = max(times_ms) if times_ms else None
        snap.extra.update(
            {
                "http_attempts": sample_count,
                "http_successes": len(times_ms),
                "http_error_rate": 1.0 - success_rate,
                "http_median_ms": median_float(times_ms),
                "http_samples_ms": times_ms,
                "http_codes": codes,
            }
        )
        return snap
    if probe_kind == "http_by_name" and path.http_name_url:
        snap.http_ok = http_ok(runtime, src, path.http_name_url)
        snap.http_time_ms = http_time_ms(runtime, src, path.http_name_url)
        return snap
    if probe_kind == "http_body_time" and (path.http_url or path.http_name_url):
        url = path.http_url or path.http_name_url or ""
        # Use header/TTFB timing (captures pre-response application sleep).
        snap.http_time_ms = http_time_ms(runtime, src, url)
        if snap.http_time_ms is None or snap.http_time_ms < 500.0:
            body_ms = http_body_time_ms(runtime, src, url)
            if body_ms is not None:
                snap.http_time_ms = body_ms
        snap.http_ok = snap.http_time_ms is not None
        return snap
    if probe_kind == "iperf_throughput" and path.peer_host:
        peer_ip = path.dst_ip
        looked_up = exec_or_empty(
            runtime,
            path.peer_host,
            "ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -1",
        ).strip()
        if looked_up:
            peer_ip = looked_up.split("/")[0]
        if not peer_ip:
            snap.extra["bits_per_second"] = None
            snap.extra["error"] = "missing_peer_ip"
            return snap
        bps = iperf_throughput_bps(
            runtime,
            src,
            path.peer_host,
            peer_ip,
            duration_sec=3,
            port=15201,
        )
        snap.extra["bits_per_second"] = bps
        snap.extra["peer_ip"] = peer_ip
        # Always sample TBF counters: iperf may be absent on stubs while the
        # capacity fault still shapes traffic on the inject router.
        inject_host = _params_get(params, "host_name") if params is not None else None
        intf = _params_get(params, "intf_name") if params is not None else None
        check_host = inject_host or src
        check_intf = intf or "eth0"
        if bps is None:
            runtime.exec(
                src,
                f"timeout 2s bash -c 'while true; do printf %01024d 1; done "
                f"| nc -u -w1 {peer_ip} 9' >/dev/null 2>&1 || true",
                timeout=8,
            )
        overlimits = tbf_overlimits(runtime, check_host, check_intf)
        snap.extra["tbf_overlimits"] = overlimits
        return snap
    if probe_kind == "route_get_onlink":
        onlink_dst = path.dst_ip
        if onlink_dst:
            onlink_dst = _onlink_probe_dst(runtime, src, onlink_dst) or onlink_dst
        if not onlink_dst:
            snap.extra["route_onlink"] = None
            snap.extra["error"] = "missing_onlink_dst"
            return snap
        onlink = route_is_onlink(runtime, src, onlink_dst)
        snap.extra["route_onlink"] = onlink
        snap.extra["onlink_dst"] = onlink_dst
        if path.dst_ip:
            snap.ping_ok = ping_ok(runtime, src, path.dst_ip)
        return snap
    if probe_kind == "ping_old_ip":
        inject_host = _params_get(params, "host_name") or src
        old_ip = path.old_ip
        if not old_ip:
            old_ip = exec_or_empty(
                runtime, inject_host, "cat /tmp/nika_original_ip 2>/dev/null || true"
            ).strip()
        peer = path.peer_host
        if not peer or peer == inject_host:
            peer = None
        if not peer and inject_host:
            site = inject_host.split("_", 1)[0]
            if site and not inject_host.endswith("_edge"):
                edge = f"{site}_edge"
                nodes = runtime.list_nodes() or []
                if edge in nodes:
                    peer = edge
        if peer and old_ip:
            snap.ping_ok = ping_ok(runtime, peer, old_ip.split("/")[0])
            snap.symptom_ok = snap.ping_ok
            snap.extra["old_ip"] = old_ip
            snap.extra["peer_host"] = peer
        else:
            snap.ping_ok = True  # fail closed: cannot verify without peer/old_ip
            snap.extra["error"] = "missing_peer_or_old_ip"
        return snap
    if probe_kind == "isolation_http":
        if path.symptom_url:
            snap.symptom_ok = http_ok(runtime, src, path.symptom_url)
        if path.control_url:
            snap.control_ok = http_ok(runtime, src, path.control_url)
        # CoreDNS isolation: host nslookup against the Service ClusterIP from the
        # filtered node (src is rewritten to a CoreDNS host in evaluate_symptom /
        # pre_inject). Prefer this over kubectl-exec pod probes — distroless llm-d
        # images and nested quoting make pod DNS flaky, while verify_fault already
        # covers the workload-pod signal.
        if snap.symptom_ok is None and _params_get(params, "dns_service"):
            control = _params_get(params, "control_node") or "controller"
            k8s = getattr(runtime, "lab_api", None)
            dns_ns = _params_get(params, "dns_namespace") or "kube-system"
            dns_svc = _params_get(params, "dns_service") or "kube-dns"
            cluster_ip = exec_or_empty(
                runtime,
                control,
                f"kubectl get svc {dns_svc} -n {dns_ns} "
                "-o jsonpath={.spec.clusterIP} 2>/dev/null || true",
                timeout=20,
            ).strip()
            if k8s is not None and cluster_ip and hasattr(k8s, "k8s_host_dns_ok"):
                snap.symptom_ok = k8s.k8s_host_dns_ok(src, cluster_ip)
            else:
                snap.symptom_ok = _cluster_dns_ok(
                    runtime, src, server=cluster_ip or None
                )
            # Leave control_ok unset: isolation compare treats missing control as
            # intact. An external dst_ip ping from a k3s node is not a meaningful
            # sibling path for DNS isolation.
        return snap
    if probe_kind == "control_plane_bgp" and path.control_plane_host:
        neighbor_ip = _params_get(params, "neighbor_ip") if params is not None else None
        if neighbor_ip:
            # Session-scoped faults (e.g. max-prefix) leave other peers Established.
            neighbor_out = exec_or_empty(
                runtime,
                path.control_plane_host,
                f"vtysh -c 'show bgp neighbors {neighbor_ip}' 2>/dev/null || true",
                timeout=20,
            )
            snap.control_plane_ok = any(
                "bgp state" in line.lower() and "established" in line.lower()
                for line in neighbor_out.splitlines()
            )
        else:
            snap.control_plane_ok = frr_bgp_has_established_session(
                runtime, path.control_plane_host
            )
        return snap
    if probe_kind == "control_plane_ospf" and path.control_plane_host:
        output = runtime.exec(
            path.control_plane_host,
            "vtysh -c 'show ip ospf neighbor' 2>/dev/null || true",
            timeout=15,
        )
        snap.control_plane_ok = "Full" in output or "full" in output.lower()
        return snap
    if path.dst_ip:
        snap.ping_ok = ping_ok(runtime, src, path.dst_ip)
    if path.http_url:
        snap.http_ok = http_ok(runtime, src, path.http_url)
    return snap


def symptom_class_to_expect(symptom_class: SymptomClass) -> str:
    mapping = {
        "unreachable": "unreachable",
        "loss": "loss_increased",
        "latency": "latency_increased",
        "gray": "gray_loss",
        "control_plane": "control_plane_down",
        "isolation": "isolation",
        "degradation": "degraded",
        "none": "none",
    }
    return mapping.get(symptom_class, "unreachable")
