"""Failure-specific assertions for parametrized failure E2E tests."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from nika.problems.link_interface.link import _resolve_link_intf
from nika.net_env.verify import ping_ok
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy


@dataclass
class FailureE2EContext:
    problem_name: str
    scenario: str
    topo_size: str
    problem: Any
    parsed: Any
    runtime: Any
    verify: dict[str, Any] | None = None
    symptom: dict[str, Any] | None = None
    recovered: dict[str, Any] | None = None
    before: Any = None
    original_nano: int | None = None


_LOW_BPS_MAX = 100_000.0
_IPERF_DURATION_SEC = 5


def _ctx_intf(ctx: FailureE2EContext) -> str:
    return _resolve_link_intf(ctx.parsed.intf_name, "kathara")


def _require_probe_path(ctx: FailureE2EContext):
    from tests.support.symptom.probe import _resolve_path

    path = _resolve_path(ctx.scenario, ctx.parsed, topo_size=ctx.topo_size or "s")
    assert path is not None and path.dst_ip
    return path


def _wait_for_ping(runtime, src: str, dst: str, timeout_sec: float = 90.0) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if ping_ok(runtime, src, dst):
            return
        time.sleep(2.0)
    assert ping_ok(runtime, src, dst)


def _capture_nano_cpus_pre(ctx: FailureE2EContext) -> None:
    from nika.problems.support.cpu_quota_helpers import read_nano_cpus

    ctx.original_nano = read_nano_cpus(ctx.runtime, ctx.parsed.host_name)


def _stash_original_nano(ctx: FailureE2EContext) -> None:
    if ctx.original_nano is not None and ctx.symptom is not None:
        ctx.symptom.setdefault("details", {})["_original_nano"] = ctx.original_nano


def _qdisc(runtime, node: str, intf: str) -> str:
    return runtime.exec(node, f"tc qdisc show dev {intf} 2>/dev/null || true").lower()


def assert_link_down(ctx: FailureE2EContext) -> None:
    assert ctx.verify is not None
    assert ctx.verify["details"]["operstate"] == "down", ctx.verify
    host_qdisc = _qdisc(ctx.runtime, ctx.parsed.host_name, _ctx_intf(ctx))
    assert "netem" not in host_qdisc, host_qdisc
    assert ctx.symptom is not None
    assert ctx.symptom["comparison"]["operstate"] == "down", ctx.symptom
    from nika.workflows.benchmark.isp_options import is_isp_scenario

    # Meshed ISP IGP keeps an alternate path; evaluate_symptom already treats
    # operstate as the authoritative link_down signal there.
    if not is_isp_scenario(ctx.scenario):
        assert ctx.symptom.get("after", {}).get("ping_ok") is False, ctx.symptom


def assert_link_down_recover(ctx: FailureE2EContext) -> None:
    assert (
        ctx.runtime.get_interface_operstate(ctx.parsed.host_name, _ctx_intf(ctx))
        == "up"
    )


def assert_mtu_mismatch(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import ping_df_probe, ping_mtu_frag_needed

    path = _require_probe_path(ctx)
    src, dst = path.src_host, path.dst_ip
    small_ok, _, small_raw = ping_df_probe(ctx.runtime, src, dst, packet_size=64)
    large_ok, _, large_raw = ping_df_probe(ctx.runtime, src, dst, packet_size=1400)
    assert small_ok is True, {
        "src": src,
        "dst": dst,
        "packet_size": 64,
        "raw": small_raw,
    }
    assert large_ok is False, {
        "src": src,
        "dst": dst,
        "packet_size": 1400,
        "raw": large_raw,
    }
    assert ping_mtu_frag_needed(ctx.runtime, src, dst) is True, {
        "src": src,
        "dst": dst,
        "host": getattr(ctx.parsed, "host_name", None),
        "intf": getattr(ctx.parsed, "intf_name", None),
    }


def assert_link_detach_pre(ctx: FailureE2EContext) -> None:
    path = _require_probe_path(ctx)
    assert ctx.runtime.interface_exists(ctx.parsed.host_name, _ctx_intf(ctx))
    _wait_for_ping(ctx.runtime, path.src_host, path.dst_ip)


def assert_link_detach(ctx: FailureE2EContext) -> None:
    assert ctx.verify is not None
    assert ctx.verify["details"]["artifact"]["verified"] is True, ctx.verify
    assert ctx.verify["details"]["symptom"]["verified"] is True, ctx.verify
    assert ctx.symptom is not None
    assert ctx.symptom.get("after", {}).get("ping_ok") is False, ctx.symptom
    assert ctx.symptom["comparison"]["interface_gone"] is True, ctx.symptom


def assert_link_detach_recover(ctx: FailureE2EContext) -> None:
    assert ctx.runtime.interface_exists(ctx.parsed.host_name, _ctx_intf(ctx))


def assert_link_flap_pre(ctx: FailureE2EContext) -> None:
    from tests.support.symptom.flap_probes import assert_baseline_healthy
    from tests.support.symptom.probe import _resolve_path

    path = _resolve_path(ctx.scenario, ctx.parsed, topo_size=ctx.topo_size or "s")
    assert path is not None and path.dst_ip
    baseline_ok, baseline = assert_baseline_healthy(ctx.runtime, path)
    assert baseline_ok is True, baseline


def assert_link_flap(ctx: FailureE2EContext) -> None:
    assert ctx.symptom is not None
    assert ctx.symptom.get("comparison", {}).get("periodic_loss") is True, ctx.symptom


def _link_peer(runtime, host: str, intf: str) -> tuple[str, str] | None:
    controller = KatharaVdeFaultProxy(runtime)
    state = controller.discover(host, intf)
    if state is None:
        return None
    if state.endpoint.node == host and state.endpoint.intf == intf:
        return state.peer.node, state.peer.intf
    return state.endpoint.node, state.endpoint.intf


def assert_link_capacity_bottleneck_pre(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import iperf_throughput_bps

    from tests.support.symptom.probe import _resolve_path

    path = _resolve_path(ctx.scenario, ctx.parsed, topo_size=ctx.topo_size or "s")
    assert path is not None and path.peer_host and path.dst_ip
    baseline_bps = iperf_throughput_bps(
        ctx.runtime,
        path.src_host,
        path.peer_host,
        path.dst_ip,
        duration_sec=_IPERF_DURATION_SEC,
        port=15211,
    )
    assert baseline_bps is not None and baseline_bps > _LOW_BPS_MAX, baseline_bps
    ctx.before = {"baseline_bps": baseline_bps, "path": path}


def assert_link_capacity_bottleneck(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import iperf_throughput_bps, ping_stats

    baseline = ctx.before
    path = baseline["path"]
    baseline_bps = baseline["baseline_bps"]
    src, peer, peer_ip = path.src_host, path.peer_host, path.dst_ip
    resolved_intf = _ctx_intf(ctx)
    host_qdisc = _qdisc(ctx.runtime, ctx.parsed.host_name, resolved_intf)
    assert "tbf" not in host_qdisc and "netem" not in host_qdisc, host_qdisc

    peer_ep = _link_peer(ctx.runtime, ctx.parsed.host_name, resolved_intf)
    assert peer_ep is not None
    peer_qdisc = _qdisc(ctx.runtime, peer_ep[0], peer_ep[1])
    assert "tbf" not in peer_qdisc and "netem" not in peer_qdisc, peer_qdisc

    controller = KatharaVdeFaultProxy(ctx.runtime)
    proxy = controller.discover(ctx.parsed.host_name, resolved_intf)
    assert proxy is not None
    assert controller.tbf_configured(proxy)

    # Ping before iperf: a 30kbit TBF fills under load and would drop replies.
    after_ping = ping_stats(ctx.runtime, src, peer_ip, count=5, interval_sec=0.2)
    assert after_ping.received > 0
    injected_bps = iperf_throughput_bps(
        ctx.runtime,
        src,
        peer,
        peer_ip,
        duration_sec=_IPERF_DURATION_SEC,
        port=15212,
    )
    if injected_bps is not None:
        assert float(injected_bps) < _LOW_BPS_MAX or (
            float(injected_bps) < float(baseline_bps) * 0.5
        ), (baseline_bps, injected_bps)


def assert_link_capacity_bottleneck_recover(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import iperf_throughput_bps

    from tests.support.symptom.probe import _resolve_path

    path = _resolve_path(ctx.scenario, ctx.parsed, topo_size=ctx.topo_size or "s")
    assert path is not None and path.peer_host and path.dst_ip
    restored_bps = iperf_throughput_bps(
        ctx.runtime,
        path.src_host,
        path.peer_host,
        path.dst_ip,
        duration_sec=_IPERF_DURATION_SEC,
        port=15213,
    )
    assert restored_bps is not None and float(restored_bps) > _LOW_BPS_MAX


def assert_load_balancer_overload(ctx: FailureE2EContext) -> None:
    _stash_original_nano(ctx)
    artifact = ctx.verify["details"]
    assert artifact["load_running"] is True
    assert artifact["capacity_ok"] is True
    assert artifact["nginx_running"] is True
    details = ctx.symptom["details"]
    assert details["nginx_saturated"] is True
    assert details["vip_degraded"] is True
    assert details["control_ok"] is True
    assert details["backend_ok_gate"] is True
    assert details["path_ok"] is True


def assert_load_balancer_overload_recover(ctx: FailureE2EContext) -> None:
    from nika.problems.support.cpu_quota_helpers import read_nano_cpus

    assert ctx.symptom is not None
    original_nano = ctx.symptom.get("details", {}).get("_original_nano")
    if original_nano is not None:
        assert read_nano_cpus(ctx.runtime, ctx.parsed.host_name) == original_nano


def assert_sender_resource_contention_pre(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import http_download_stats, ping_stats
    from nika.problems.support.cpu_quota_helpers import read_nano_cpus

    ctx.original_nano = read_nano_cpus(ctx.runtime, ctx.parsed.host_name)
    # Clos/SDN nginx only serves index.html; stage /small.bin before the probe.
    ensure = getattr(ctx.problem, "_ensure_http_objects", None)
    if callable(ensure):
        ensure(ctx.parsed)
    small_ok = http_download_stats(
        ctx.runtime, ctx.parsed.client_host, ctx.parsed.small_url, max_time_sec=30
    )
    assert small_ok.ok, small_ok.raw
    healthy_rtt = ping_stats(
        ctx.runtime,
        ctx.parsed.client_host,
        ctx.parsed.dst_ip,
        count=10,
        interval_sec=0.2,
    )
    assert healthy_rtt.loss_percent < 5.0
    assert healthy_rtt.rtt_avg_ms is not None
    ctx.before = healthy_rtt


def assert_sender_resource_contention(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import http_download_stats, ping_stats

    _stash_original_nano(ctx)
    artifact = ctx.verify["details"]
    assert artifact["stress_running"] is True
    assert artifact["quota_ok"] is True
    assert artifact.get("cpu_http_running", True) is True
    details = ctx.symptom["details"]
    assert details["path_ok"] is True
    assert details["perf_ok"] is True
    assert details["throughput_ratio"] is not None or details["time_ratio"] is not None
    if details["throughput_ratio"] is not None:
        assert (
            details["throughput_ratio"] <= 0.20 or (details["time_ratio"] or 0) >= 5.0
        )
    fault_rtt = ping_stats(
        ctx.runtime,
        ctx.parsed.client_host,
        ctx.parsed.dst_ip,
        count=10,
        interval_sec=0.2,
    )
    # Path health under CPU starvation: low loss + small HTTP. ICMP RTT to the
    # contended host is expected to inflate and is not a path-failure signal.
    assert fault_rtt.loss_percent < 5.0
    assert fault_rtt.rtt_avg_ms is not None
    small_fault = http_download_stats(
        ctx.runtime, ctx.parsed.client_host, ctx.parsed.small_url, max_time_sec=30
    )
    assert small_fault.ok, small_fault.raw


def assert_sender_resource_contention_recover(ctx: FailureE2EContext) -> None:
    from nika.problems.endpoint_application.transport import (
        _RECOVER_THROUGHPUT_MIN_RATIO,
    )
    from nika.problems.support.cpu_quota_helpers import read_nano_cpus

    original_nano = ctx.symptom.get("details", {}).get("_original_nano")
    if original_nano is None:
        original_nano = read_nano_cpus(ctx.runtime, ctx.parsed.host_name)
    assert read_nano_cpus(ctx.runtime, ctx.parsed.host_name) == original_nano
    assert not ctx.runtime.process_running(ctx.parsed.host_name, "stress-ng")
    assert ctx.recovered is not None
    details = ctx.recovered["details"]
    restored_bps = details["restored_throughput_bps"]
    baseline_bps = details["baseline_throughput_bps"]
    assert restored_bps is not None and baseline_bps
    assert restored_bps >= _RECOVER_THROUGHPUT_MIN_RATIO * baseline_bps, (
        f"restored={restored_bps:.0f} baseline={baseline_bps:.0f}"
    )


def assert_bmv2_switch_down_pre(ctx: FailureE2EContext) -> None:
    import time

    from nika.net_env.verify import http_ok

    path = _require_probe_path(ctx)
    # Gateway VIP is L4-translated TCP/80 and does not answer ICMP. Fabric
    # webs are pingable; prefer HTTP when the probe path has a URL.
    for _ in range(8):
        if path.http_url and http_ok(ctx.runtime, path.src_host, path.http_url):
            return
        if path.dst_ip and ping_ok(ctx.runtime, path.src_host, path.dst_ip):
            return
        time.sleep(1.0)
    if path.http_url:
        assert http_ok(ctx.runtime, path.src_host, path.http_url), path
    else:
        assert ping_ok(ctx.runtime, path.src_host, path.dst_ip)


def assert_bmv2_switch_down(ctx: FailureE2EContext) -> None:
    assert ctx.symptom is not None
    after = ctx.symptom.get("after", {})
    assert after.get("http_ok") is False or after.get("ping_ok") is False, ctx.symptom


def assert_incast_pre(ctx: FailureE2EContext) -> None:
    from tests.support.symptom import get_symptom_contract
    from tests.support.symptom.probe import _resolve_path, run_probe_snapshot

    path = _resolve_path(ctx.scenario, ctx.parsed, topo_size=ctx.topo_size)
    assert path is not None and path.dst_ip
    contract = get_symptom_contract(ctx.problem_name)
    before = run_probe_snapshot(ctx.runtime, contract.probe, path, params=ctx.parsed)
    assert before.ping_ok is True, before.as_dict()
    assert before.rtt_avg_ms is not None and before.rtt_avg_ms > 0
    ctx.before = before


def assert_receiver_resource_contention_pre(ctx: FailureE2EContext) -> None:
    """Ensure the receiver can fetch a large object before contention."""
    from nika.net_env.verify import http_download_stats

    ensure = getattr(ctx.problem, "_ensure_peer_large_object", None)
    url = ensure(ctx.parsed) if callable(ensure) else None
    if not url:
        path = _require_probe_path(ctx)
        assert path is not None and path.http_url, path
        url = path.http_url
    stats = http_download_stats(ctx.runtime, ctx.parsed.host_name, url, max_time_sec=60)
    assert stats.ok, stats.raw
    ctx.before = stats


def assert_isolation_http_pre(ctx: FailureE2EContext) -> None:
    """Capture healthy isolation probe (symptom up, control up when probed)."""
    from tests.support.symptom import get_symptom_contract
    from tests.support.symptom.probe import _resolve_path, run_probe_snapshot

    if ctx.problem_name == "k8s_coredns_isolated":
        k8s = ctx.problem.runtime.lab_api
        devices = ctx.problem._target_devices(ctx.parsed, k8s)
        if devices:
            ctx.parsed.symptom_host = devices[0]
    path = _resolve_path(ctx.scenario, ctx.parsed, topo_size=ctx.topo_size)
    assert path is not None
    contract = get_symptom_contract(ctx.problem_name)
    before = run_probe_snapshot(ctx.runtime, contract.probe, path, params=ctx.parsed)
    assert before.symptom_ok is True, before.as_dict()
    if before.control_ok is not None:
        assert before.control_ok is True, before.as_dict()
    ctx.before = before


def assert_lb_connection_state_exhaustion_pre(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import http_ok

    path = _require_probe_path(ctx)
    assert path.http_url is not None
    assert http_ok(ctx.runtime, path.src_host, path.http_url), path


def assert_lb_connection_state_exhaustion(ctx: FailureE2EContext) -> None:
    assert ctx.symptom is not None
    details = ctx.symptom["details"]
    assert details["affinity_broken"] is True, ctx.symptom
    assert details["vip_ok"] is True, ctx.symptom


def assert_tcp_rwnd_pre(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import (
        http_download_stats,
        iperf_throughput_bps,
        median_throughput_bps,
        ping_stats,
    )
    from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import (
        primary_ipv4,
        read_sysctl_snapshot,
    )

    receiver = ctx.parsed.host_name
    sender_host = ctx.parsed.sender_host
    sender_ip = ctx.parsed.sender_ip
    small_url = ctx.parsed.small_url
    large_url = ctx.parsed.large_url
    receiver_ip = primary_ipv4(ctx.runtime, receiver)
    assert receiver_ip

    small_ok = None
    for _ in range(30):
        small_ok = http_download_stats(
            ctx.runtime, receiver, small_url, max_time_sec=30
        )
        if small_ok.ok:
            break
        time.sleep(2)
    assert small_ok is not None and small_ok.ok, (
        small_ok.raw if small_ok else "no_probe"
    )

    healthy_rtt = ping_stats(
        ctx.runtime, receiver, sender_ip, count=5, interval_sec=0.2
    )
    assert healthy_rtt.rtt_avg_ms is not None
    assert healthy_rtt.rtt_avg_ms > 30.0
    assert healthy_rtt.loss_percent < 5.0

    healthy_bps = iperf_throughput_bps(
        ctx.runtime, sender_host, receiver, receiver_ip, duration_sec=3
    )
    assert healthy_bps is not None and healthy_bps > 0

    healthy_http = median_throughput_bps(
        ctx.runtime,
        receiver,
        large_url,
        trials=1,
        max_time_sec=60,
        max_bytes=2 * 1024 * 1024,
    )
    assert healthy_http is not None and healthy_http > 0
    ctx.before = {
        "healthy_rtt": healthy_rtt,
        "healthy_bps": healthy_bps,
        "healthy_http": healthy_http,
        "receiver_ip": receiver_ip,
        "_original_sysctl": read_sysctl_snapshot(ctx.runtime, receiver),
    }


def assert_tcp_rwnd(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import (
        http_download_stats,
        iperf_throughput_bps,
        median_throughput_bps,
        ping_stats,
    )
    from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import sysctl_get

    baseline = ctx.before
    receiver = ctx.parsed.host_name
    sender_host = ctx.parsed.sender_host
    sender_ip = ctx.parsed.sender_ip
    small_url = ctx.parsed.small_url
    large_url = ctx.parsed.large_url
    receiver_ip = baseline["receiver_ip"]

    assert sysctl_get(ctx.runtime, receiver, "net.ipv4.tcp_moderate_rcvbuf") == "0"

    small_fault = http_download_stats(ctx.runtime, receiver, small_url, max_time_sec=30)
    assert small_fault.ok, small_fault.raw
    fault_rtt = ping_stats(ctx.runtime, receiver, sender_ip, count=5, interval_sec=0.2)
    assert fault_rtt.loss_percent < 5.0
    assert fault_rtt.rtt_avg_ms is not None
    assert fault_rtt.rtt_avg_ms < baseline["healthy_rtt"].rtt_avg_ms * 1.5

    fault_bps = iperf_throughput_bps(
        ctx.runtime, sender_host, receiver, receiver_ip, duration_sec=3
    )
    assert fault_bps is not None
    assert fault_bps / baseline["healthy_bps"] < 0.5

    fault_http = median_throughput_bps(
        ctx.runtime,
        receiver,
        large_url,
        trials=1,
        max_time_sec=60,
        max_bytes=2 * 1024 * 1024,
    )
    assert fault_http is not None
    assert fault_http / baseline["healthy_http"] < 0.5

    target = ctx.verify["details"]["target_buffer_bytes"]
    rmem = sysctl_get(ctx.runtime, receiver, "net.ipv4.tcp_rmem")
    assert str(target) in rmem.replace("\t", " ")


def assert_tcp_rwnd_recover(ctx: FailureE2EContext) -> None:
    from nika.net_env.verify import iperf_throughput_bps
    from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import (
        read_sysctl_snapshot,
    )

    baseline = ctx.before
    receiver = ctx.parsed.host_name
    sender_host = ctx.parsed.sender_host
    receiver_ip = baseline["receiver_ip"]
    original = baseline.get("_original_sysctl")
    if original is not None:
        restored = read_sysctl_snapshot(ctx.runtime, receiver)
        assert restored.moderate_rcvbuf == original.moderate_rcvbuf

    restored_bps = iperf_throughput_bps(
        ctx.runtime, sender_host, receiver, receiver_ip, duration_sec=3
    )
    assert restored_bps is not None
    assert restored_bps / baseline["healthy_bps"] >= 0.8


HOOKS: dict[str, dict[str, Any]] = {
    "link_down": {
        "post_inject": assert_link_down,
        "post_recover": assert_link_down_recover,
    },
    "mtu_mismatch": {"post_inject": assert_mtu_mismatch},
    "link_detach": {
        "pre_inject": assert_link_detach_pre,
        "post_inject": assert_link_detach,
        "post_recover": assert_link_detach_recover,
    },
    "link_flap": {
        "pre_inject": assert_link_flap_pre,
        "post_inject": assert_link_flap,
    },
    "link_capacity_bottleneck": {
        "pre_inject": assert_link_capacity_bottleneck_pre,
        "post_inject": assert_link_capacity_bottleneck,
        "post_recover": assert_link_capacity_bottleneck_recover,
    },
    "load_balancer_overload": {
        "pre_inject": _capture_nano_cpus_pre,
        "post_inject": assert_load_balancer_overload,
        "post_recover": assert_load_balancer_overload_recover,
    },
    "sender_resource_contention": {
        "pre_inject": assert_sender_resource_contention_pre,
        "post_inject": assert_sender_resource_contention,
        "post_recover": assert_sender_resource_contention_recover,
    },
    "bmv2_switch_down": {
        "pre_inject": assert_bmv2_switch_down_pre,
        "post_inject": assert_bmv2_switch_down,
    },
    "incast_traffic_network_limitation": {
        "pre_inject": assert_incast_pre,
    },
    "receiver_resource_contention": {
        "pre_inject": assert_receiver_resource_contention_pre,
    },
    "k8s_networkpolicy_deny": {
        "pre_inject": assert_isolation_http_pre,
    },
    "k8s_coredns_isolated": {
        "pre_inject": assert_isolation_http_pre,
    },
    "lb_connection_state_exhaustion": {
        "pre_inject": assert_lb_connection_state_exhaustion_pre,
        "post_inject": assert_lb_connection_state_exhaustion,
    },
    "tcp_receive_window_limited": {
        "pre_inject": assert_tcp_rwnd_pre,
        "post_inject": assert_tcp_rwnd,
        "post_recover": assert_tcp_rwnd_recover,
    },
}
