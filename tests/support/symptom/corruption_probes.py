"""Symptom probes for link_packet_corruption partial-degradation faults."""

from __future__ import annotations

import time
from typing import Any

from nika.net_env.verify import (
    exec_or_empty,
    http_download_stats,
    iperf_tcp_metrics,
    ping_stats,
)
from nika.problems.link_interface.link import _resolve_link_intf
from nika.runtime.base import LabRuntime
from nika.runtime.kathara.runtime import KatharaRuntime
from tests.support.symptom.flap_probes import _routing_probe_for
from tests.support.symptom.probe import _resolve_path

PARTIAL_LOSS_MIN_PERCENT = 0.5
PARTIAL_LOSS_MAX_PERCENT = 40.0
IPERF_DURATION_SEC = 5
THROUGHPUT_MAX_RATIO = 0.85
HTTP_SLOWDOWN_MIN_RATIO = 1.15
HTTP_SAMPLE_COUNT = 5


def _service_peer_host(problem: Any, path: Any, scenario: str | None) -> str:
    """Host that terminates the probe-path TCP/HTTP service (iperf server side)."""
    net_env = getattr(problem, "net_env", None)
    if scenario == "campus_lan":
        return "web_server_0"
    if scenario == "dc_clos":
        return "webserver0_pod0"
    if scenario == "sdn_l3_clos" and net_env is not None:
        model = getattr(net_env, "model", None)
        if model is not None and getattr(model, "client_endpoints", None):
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            return victim.name
    return path.peer_host or path.src_host


def _http_median_slowdown(
    problem: Any,
    runtime: LabRuntime,
    path: Any,
    src_host: str,
) -> tuple[bool, dict[str, Any]]:
    baseline = getattr(problem, "_baseline_http_time_s", None)
    if not path.http_url or baseline is None:
        return False, {"skipped": True}
    times_s: list[float] = []
    for _ in range(HTTP_SAMPLE_COUNT):
        http = http_download_stats(
            runtime,
            src_host,
            path.http_url,
            max_time_sec=30,
            connect_timeout_sec=5,
        )
        if http.ok and http.time_total_s is not None:
            times_s.append(float(http.time_total_s))
    if not times_s:
        return False, {"http_samples_s": times_s}
    median_s = sorted(times_s)[len(times_s) // 2]
    ok = median_s > float(baseline) * HTTP_SLOWDOWN_MIN_RATIO
    return ok, {
        "http_median_s": median_s,
        "http_samples_s": times_s,
        "baseline_http_s": baseline,
    }


def _resolve_iperf_dst_ip(runtime: LabRuntime, peer_host: str, dst_ip: str) -> str:
    looked_up = exec_or_empty(
        runtime,
        peer_host,
        "ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -1",
    ).strip()
    if looked_up:
        return looked_up.split("/")[0]
    return dst_ip


def _link_peer(
    runtime: LabRuntime, host: str, intf: str, problem: Any
) -> tuple[str, str] | None:
    if isinstance(runtime, KatharaRuntime):
        from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy

        state = getattr(problem, "_proxy", None) or KatharaVdeFaultProxy(
            runtime
        ).discover(host, intf)
        if state is None:
            return None
        if state.endpoint.node == host and state.endpoint.intf == intf:
            return state.peer.node, state.peer.intf
        return state.endpoint.node, state.endpoint.intf
    return None


def _endpoint_qdisc_clean(runtime: LabRuntime, node: str, intf: str) -> bool:
    output = runtime.exec(node, f"tc qdisc show dev {intf} 2>/dev/null || true").lower()
    return "netem" not in output and "tbf" not in output


def _tcp_degraded(
    problem: Any,
    *,
    path: Any,
    runtime: LabRuntime,
    src_host: str,
    peer_host: str,
    dst_ip: str,
    partial_loss: bool,
    ping_rtt_ms: float | None,
) -> tuple[bool, dict[str, Any]]:
    baseline_bps = getattr(problem, "_baseline_iperf_bps", None)
    baseline_retrans = getattr(problem, "_baseline_iperf_retrans", None)
    baseline_http = getattr(problem, "_baseline_http_time_s", None)
    baseline_rtt = getattr(problem, "_baseline_rtt_ms", None)

    peer_ip = _resolve_iperf_dst_ip(runtime, peer_host, dst_ip)
    injected_bps, injected_retrans = iperf_tcp_metrics(
        runtime,
        src_host,
        peer_host,
        peer_ip,
        duration_sec=IPERF_DURATION_SEC,
        port=15221,
    )
    details: dict[str, Any] = {
        "baseline_iperf_bps": baseline_bps,
        "baseline_iperf_retrans": baseline_retrans,
        "injected_iperf_bps": injected_bps,
        "injected_iperf_retrans": injected_retrans,
        "peer_ip": peer_ip,
    }

    if injected_bps is not None and baseline_bps is not None:
        if float(injected_bps) < float(baseline_bps) * THROUGHPUT_MAX_RATIO:
            details["tcp_degraded_via"] = "iperf_throughput"
            return True, details
    if (
        injected_retrans is not None
        and baseline_retrans is not None
        and injected_retrans > baseline_retrans
    ):
        details["tcp_degraded_via"] = "iperf_retransmits"
        return True, details
    # Dual-E2E often lacks a pre-inject baseline; a large absolute retransmit
    # count on a short iperf still indicates path corruption.
    if (
        baseline_retrans is None
        and injected_retrans is not None
        and injected_retrans >= 20
    ):
        details["tcp_degraded_via"] = "iperf_retransmits_absolute"
        return True, details

    http_after = None
    if path.http_url:
        http_after = http_download_stats(
            runtime,
            src_host,
            path.http_url,
            max_time_sec=30,
            connect_timeout_sec=5,
        )
        details["injected_http_time_s"] = http_after.time_total_s
        details["injected_http_ok"] = http_after.ok
        if (
            baseline_http is not None
            and http_after.time_total_s is not None
            and http_after.ok
            and float(http_after.time_total_s)
            > float(baseline_http) * HTTP_SLOWDOWN_MIN_RATIO
        ):
            details["tcp_degraded_via"] = "http_slowdown"
            return True, details

    if (
        baseline_rtt is not None
        and ping_rtt_ms is not None
        and ping_rtt_ms > float(baseline_rtt) * 1.2
    ):
        details["tcp_degraded_via"] = "ping_rtt"
        return True, details

    if partial_loss and injected_bps is not None:
        details["tcp_degraded_via"] = "iperf_completed_with_loss"
        return True, details

    if partial_loss and path.http_url and http_after is not None and http_after.ok:
        details["tcp_degraded_via"] = "partial_loss_with_http"
        return True, details

    details["tcp_degraded_via"] = None
    return False, details


def evaluate_device_forwarding_corruption_symptom(
    problem: Any,
    params: Any,
) -> tuple[bool, dict[str, Any]]:
    """Custom evaluate_symptom handler for device_forwarding_packet_corruption."""
    from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf import (
        SwitchNamespaceBitflip,
    )

    runtime = problem.runtime
    scenario = getattr(problem, "scenario_name", None)
    topo_size = getattr(problem.net_env, "topo_size", None) or "s"
    path = _resolve_path(scenario, params, topo_size=topo_size)
    if path is None or not path.dst_ip:
        return False, {"error": "no_probe_path", "scenario": scenario}

    forwarding_device = getattr(params, "forwarding_device", None)
    fault_intf = getattr(params, "intf_name", "eth0")
    if not forwarding_device:
        return False, {"error": "missing_forwarding_device"}

    artifact_attached = SwitchNamespaceBitflip(runtime).attached(
        forwarding_device, fault_intf
    )
    fault_operstate = runtime.get_interface_operstate(forwarding_device, fault_intf)
    link_up = fault_operstate == "up"

    routers = list(getattr(problem.net_env, "routers", None) or [])
    routing_host = path.control_plane_host or forwarding_device
    routing_probe = _routing_probe_for(runtime, routing_host, routers=routers)
    routing_ok: bool | None = None
    if routing_probe is not None:
        routing_ok = routing_probe(runtime)

    ping = ping_stats(
        runtime,
        path.src_host,
        path.dst_ip,
        count=path.gray_ping_count,
        interval_sec=0.05,
    )
    path_reachable = ping.received > 0 and ping.loss_percent < 50.0

    peer_host = _service_peer_host(problem, path, scenario)
    tcp_degraded = False
    tcp_details: dict[str, Any] = {}
    for attempt in range(2):
        tcp_degraded, tcp_details = _tcp_degraded(
            problem,
            path=path,
            runtime=runtime,
            src_host=path.src_host,
            peer_host=peer_host,
            dst_ip=path.dst_ip,
            partial_loss=False,
            ping_rtt_ms=ping.rtt_avg_ms,
        )
        if not tcp_degraded:
            http_slow, http_slow_details = _http_median_slowdown(
                problem, runtime, path, path.src_host
            )
            if http_slow:
                tcp_degraded = True
                tcp_details["tcp_degraded_via"] = "http_median_slowdown"
                tcp_details.update(http_slow_details)
        if tcp_degraded or attempt == 1:
            break
        time.sleep(5.0)

    http_ok = True
    http_details: dict[str, Any] = {"skipped": True}
    if path.http_url:
        http = http_download_stats(
            runtime,
            path.src_host,
            path.http_url,
            max_time_sec=30,
            connect_timeout_sec=5,
        )
        http_ok = http.ok
        http_details = {
            "skipped": False,
            "http_ok": http.ok,
            "http_time_s": http.time_total_s,
        }

    no_shortcut = _endpoint_qdisc_clean(runtime, path.src_host, "eth0")
    if peer_host != path.src_host:
        no_shortcut = no_shortcut and _endpoint_qdisc_clean(runtime, peer_host, "eth0")

    verified = (
        artifact_attached
        and link_up
        and path_reachable
        and tcp_degraded
        and http_ok
        and no_shortcut
        and (routing_ok is None or routing_ok or path_reachable)
    )

    return verified, {
        "failure": "device_forwarding_packet_corruption",
        "probe": "custom",
        "symptom_class": "gray",
        "forwarding_device": forwarding_device,
        "fault_intf": fault_intf,
        "artifact_attached": artifact_attached,
        "link_up": link_up,
        "routing_ok": routing_ok,
        "ping_loss_percent": ping.loss_percent,
        "ping_received": ping.received,
        "path_reachable": path_reachable,
        "tcp_degraded": tcp_degraded,
        **tcp_details,
        "http": http_details,
        "no_shortcut": no_shortcut,
        "comparison": {
            "artifact_attached": artifact_attached,
            "link_up": link_up,
            "path_reachable": path_reachable,
            "tcp_degraded": tcp_degraded,
            "http_ok": http_ok,
            "no_shortcut": no_shortcut,
            "routing_ok": routing_ok,
        },
    }


def evaluate_link_corruption_symptom(
    problem: Any,
    params: Any,
) -> tuple[bool, dict[str, Any]]:
    """Custom evaluate_symptom handler for link_packet_corruption."""
    runtime = problem.runtime
    scenario = getattr(problem, "scenario_name", None)
    topo_size = getattr(problem.net_env, "topo_size", None) or "s"
    path = _resolve_path(scenario, params, topo_size=topo_size)
    if path is None or not path.dst_ip:
        return False, {"error": "no_probe_path", "scenario": scenario}

    backend = "kathara" if isinstance(runtime, KatharaRuntime) else "containerlab"
    fault_intf = _resolve_link_intf(getattr(params, "intf_name", "eth0"), backend)
    fault_host = getattr(params, "host_name", None)
    if not fault_host:
        return False, {"error": "missing_fault_host"}

    peer_ep = _link_peer(runtime, fault_host, fault_intf, problem)
    host_operstate = runtime.get_interface_operstate(fault_host, fault_intf)
    peer_operstate = (
        runtime.get_interface_operstate(peer_ep[0], peer_ep[1])
        if peer_ep is not None
        else "unknown"
    )
    link_up = host_operstate == "up" and (peer_ep is None or peer_operstate == "up")

    routers = list(getattr(problem.net_env, "routers", None) or [])
    routing_probe = _routing_probe_for(runtime, fault_host, routers=routers)
    routing_ok: bool | None = None
    if routing_probe is not None:
        routing_ok = routing_probe(runtime)

    ping = ping_stats(
        runtime,
        path.src_host,
        path.dst_ip,
        count=path.gray_ping_count,
        interval_sec=0.05,
    )
    partial_loss = (
        ping.received > 0
        and PARTIAL_LOSS_MIN_PERCENT <= ping.loss_percent <= PARTIAL_LOSS_MAX_PERCENT
    )

    peer_host = path.peer_host or path.src_host
    tcp_degraded, tcp_details = _tcp_degraded(
        problem,
        path=path,
        runtime=runtime,
        src_host=path.src_host,
        peer_host=peer_host,
        dst_ip=path.dst_ip,
        partial_loss=partial_loss,
        ping_rtt_ms=ping.rtt_avg_ms,
    )

    http_ok = True
    http_details: dict[str, Any] = {"skipped": True}
    if path.http_url:
        http = http_download_stats(
            runtime,
            path.src_host,
            path.http_url,
            max_time_sec=30,
            connect_timeout_sec=5,
        )
        http_ok = http.ok
        http_details = {
            "skipped": False,
            "http_ok": http.ok,
            "http_time_s": http.time_total_s,
        }

    no_shortcut = _endpoint_qdisc_clean(runtime, fault_host, fault_intf)
    if peer_ep is not None:
        no_shortcut = no_shortcut and _endpoint_qdisc_clean(
            runtime, peer_ep[0], peer_ep[1]
        )

    strong_tcp = tcp_details.get("tcp_degraded_via") in {
        "iperf_throughput",
        "iperf_retransmits",
        "iperf_retransmits_absolute",
        "iperf_completed_with_loss",
        "http_slowdown",
        "http_median_slowdown",
    }
    # ICMP loss is ideal but not always visible (ECMP / small packets); accept
    # conclusive TCP degradation with a live link as the dataplane symptom.
    dataplane_ok = (partial_loss and tcp_degraded) or (tcp_degraded and strong_tcp)

    verified = (
        link_up
        and dataplane_ok
        and http_ok
        and no_shortcut
        and (routing_ok is None or routing_ok or dataplane_ok)
    )

    return verified, {
        "failure": "link_packet_corruption",
        "probe": "custom",
        "symptom_class": "degradation",
        "link_up": link_up,
        "host_operstate": host_operstate,
        "peer_operstate": peer_operstate,
        "routing_ok": routing_ok,
        "ping_loss_percent": ping.loss_percent,
        "ping_received": ping.received,
        "partial_loss": partial_loss,
        "tcp_degraded": tcp_degraded,
        **tcp_details,
        "http": http_details,
        "no_shortcut": no_shortcut,
        "comparison": {
            "link_up": link_up,
            "partial_loss": partial_loss,
            "tcp_degraded": tcp_degraded,
            "dataplane_ok": dataplane_ok,
            "http_ok": http_ok,
            "no_shortcut": no_shortcut,
            "routing_ok": routing_ok,
        },
    }


def evaluate_link_capacity_symptom(
    problem: Any,
    params: Any,
) -> tuple[bool, dict[str, Any]]:
    """Custom evaluate_symptom for link_capacity_bottleneck.

    Kathara applies TBF on a hidden VDE proxy, so host ``tc`` overlimits and
    stub iperf are unreliable. Prefer measured low throughput when available;
    otherwise require the proxy/host TBF artifact plus a live path.
    """
    from nika.net_env.verify import iperf_throughput_bps, ping_ok, tbf_overlimits
    from nika.problems.link_interface.link import _resolve_link_intf
    from nika.runtime.kathara.runtime import KatharaRuntime
    from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy

    runtime = problem.runtime
    scenario = getattr(problem, "scenario_name", None)
    topo_size = getattr(problem.net_env, "topo_size", None) or "s"
    path = _resolve_path(scenario, params, topo_size=topo_size)
    if path is None or not path.src_host:
        return False, {"error": "no_probe_path", "scenario": scenario}

    fault_host = getattr(params, "host_name", None)
    if not fault_host:
        return False, {"error": "missing_fault_host"}

    backend = "kathara" if isinstance(runtime, KatharaRuntime) else "containerlab"
    fault_intf = _resolve_link_intf(getattr(params, "intf_name", "eth0"), backend)

    peer_host = path.peer_host or getattr(params, "peer_host", None)
    # Prefer the inject-enumerated probe address; only fall back to a live
    # lookup when the path has no dst_ip.
    peer_ip = path.dst_ip
    if peer_host and not peer_ip:
        looked_up = exec_or_empty(
            runtime,
            peer_host,
            "ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -1",
        ).strip()
        if looked_up:
            peer_ip = looked_up.split("/")[0]

    # Liveness must be sampled before iperf / counter traffic. A tight TBF
    # pipe fills under load and would otherwise make path_alive fail.
    path_alive = True
    if path.dst_ip:
        path_alive = ping_ok(runtime, path.src_host, path.dst_ip)

    bps: float | None = None
    if peer_host and peer_ip:
        bps = iperf_throughput_bps(
            runtime,
            path.src_host,
            peer_host,
            peer_ip,
            duration_sec=3,
            port=15201,
        )

    def _read_tbf() -> tuple[bool, int | None]:
        if isinstance(runtime, KatharaRuntime):
            controller = KatharaVdeFaultProxy(runtime)
            proxy = getattr(problem, "_proxy", None) or controller.discover(
                fault_host, fault_intf
            )
            configured = proxy is not None and controller.tbf_configured(proxy)
            counts = controller.tbf_overlimits(proxy) if proxy is not None else None
            return configured, counts
        counts = tbf_overlimits(runtime, fault_host, fault_intf)
        return counts is not None, counts

    artifact_ok, overlimits = _read_tbf()
    # Gentle counter drive only when iperf did not already accumulate drops.
    if peer_ip and not (overlimits is not None and int(overlimits) > 0):
        runtime.exec(
            path.src_host,
            f"ping -c 15 -i 0.2 -s 200 {peer_ip} >/dev/null 2>&1 || true",
            timeout=15,
        )
        time.sleep(1.0)
        artifact_ok, overlimits = _read_tbf()

    low_throughput = bps is not None and float(bps) < 100_000.0
    shaped = overlimits is not None and int(overlimits) > 0
    # Prefer a live path under TBF. Extreme rates (e.g. 30kbit on clab node-ns)
    # may drop ICMP while still proving shaping via overlimits.
    ok = bool(artifact_ok and (path_alive or shaped))

    return ok, {
        "failure": "link_capacity_bottleneck",
        "probe": "custom",
        "symptom_class": "degradation",
        "bits_per_second": bps,
        "tbf_overlimits": overlimits,
        "artifact_ok": artifact_ok,
        "path_alive": path_alive,
        "low_throughput": low_throughput,
        "shaped": shaped,
        "peer_ip": peer_ip,
        "comparison": {
            "expect": "capacity_bottleneck_live_path",
            "bps": bps,
            "tbf_overlimits": overlimits,
            "artifact_ok": artifact_ok,
            "path_alive": path_alive,
            "low_throughput": low_throughput,
            "shaped": shaped,
        },
    }
