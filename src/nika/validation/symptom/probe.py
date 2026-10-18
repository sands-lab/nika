"""Probe runners for test-path evaluate_symptom."""

from __future__ import annotations

import ipaddress
import re
import shlex
from dataclasses import asdict, replace
from typing import Any

from pydantic import BaseModel

from nika.net_env.verify import (
    exec_or_empty,
    http_body_time_ms,
    http_download_stats,
    http_ok,
    http_time_ms,
    median_float,
    ping_mtu_blackhole,
    ping_ok,
    ping_stats,
)
from nika.net_env.base import ProbePath
from nika.net_env.net_env_pool import get_probe_path
from nika.runtime.base import LabRuntime
from nika.validation.symptom.gray_probes import probe_gray_packet_loss
from nika.validation.symptom.types import (
    ProbeKind,
    ProbeSnapshot,
    SymptomClass,
)

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
        # Only the scenario's own destination has the scenario's ICMP address.
        icmp_ip=path.icmp_ip if dst_ip == path.dst_ip else None,
    )


def run_probe_snapshot(
    runtime: LabRuntime,
    probe_kind: ProbeKind,
    path: ProbePath,
    *,
    params: Any = None,
) -> ProbeSnapshot:
    """Measure ``probe_kind`` on ``path`` and record the endpoints probed."""
    snap = _probe_snapshot(runtime, probe_kind, path, params=params)
    snap.extra.setdefault("probe_path", asdict(path))
    return snap


def _probe_snapshot(
    runtime: LabRuntime,
    probe_kind: ProbeKind,
    path: ProbePath,
    *,
    params: Any = None,
) -> ProbeSnapshot:
    snap = ProbeSnapshot()
    src = path.src_host
    if probe_kind == "dns_answer":
        website = _params_get(params, "target_website")
        domain = _params_get(params, "target_domain")
        if not website or not domain:
            snap.extra["error"] = "missing_dns_name"
            return snap
        name = f"{website}.{domain}"
        output = exec_or_empty(
            runtime, src, f"dig +short A {shlex.quote(name)} 2>/dev/null", timeout=12
        )
        answers: list[str] = []
        for line in output.splitlines():
            value = line.strip()
            try:
                if ipaddress.ip_address(value).version == 4:
                    answers.append(value)
            except ValueError:
                continue
        snap.extra.update({"dns_name": name, "dns_answers": sorted(set(answers))})
        return snap
    if probe_kind == "gray_ping_loss" and path.dst_ip:
        # An L4-only destination (a VIP) does not answer ICMP. Probe the
        # address the scenario names for ICMP on the same path.
        if path.icmp_ip:
            path = replace(path, dst_ip=path.icmp_ip)
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
        # An L4-only destination (a VIP) is probed at its ICMP address.
        dst_ip = path.icmp_ip or path.dst_ip
        if probe_kind == "path_ping_loss":
            stats = ping_stats(runtime, src, dst_ip, count=path.ping_count)
        else:
            # Three echoes, so one lost packet does not read as a broken path.
            output = exec_or_empty(
                runtime, src, f"ping -c 3 -i 0.3 -W 2 {dst_ip}", timeout=15
            )
            received = re.search(r"(\d+) (?:packets )?received", output)
            snap.ping_ok = bool(received and int(received.group(1)) > 0)
            if received:
                snap.extra["ping_received"] = int(received.group(1))
            ttl = re.search(r"ttl=(\d+)", output)
            if snap.ping_ok and ttl:
                snap.extra["reply_ttl"] = int(ttl.group(1))
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
        lookup = exec_or_empty(
            runtime,
            src,
            f"curl -s -o /dev/null -w '%{{http_code}} %{{time_namelookup}}' "
            f"--connect-timeout 5 --max-time 20 {path.http_name_url}",
            timeout=25,
        ).strip()
        matched = re.fullmatch(r"20[06] (\d+(?:\.\d+)?)", lookup)
        if matched:
            snap.extra["name_lookup_ms"] = float(matched.group(1)) * 1000.0
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
    if probe_kind == "isolation_http":
        if path.symptom_url:
            snap.symptom_ok = http_ok(runtime, src, path.symptom_url)
        if path.control_url:
            snap.control_ok = http_ok(runtime, src, path.control_url)
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
