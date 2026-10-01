"""Directed data-plane probes for P4 gateway audit cases."""

from __future__ import annotations

import json
import shlex
import time
from typing import Any

from nika.net_env.verify import http_ok, ping_ok
from nika.problems.base import build_verify_result


def _result(problem: Any, verified: bool, details: dict[str, Any]):
    return verified, build_verify_result(problem.root_cause_name, verified, details)


def _model_endpoint(problem: Any, name: str):
    model = getattr(problem.net_env, "model", None)
    return next(
        (item for item in getattr(model, "endpoints", ()) if item.name == name), None
    )


def _observer(problem: Any, excluded: set[str]) -> str | None:
    model = getattr(problem.net_env, "model", None)
    return next(
        (
            item.name
            for item in getattr(model, "clients", ())
            if item.name not in excluded
        ),
        None,
    )


def _endpoint_reachable(problem: Any, source: str, endpoint: Any) -> bool:
    if endpoint.role == "http_service":
        return http_ok(problem.runtime, source, f"http://{endpoint.ip}/")
    return ping_ok(problem.runtime, source, endpoint.ip)


def mac_address_conflict(problem: Any, params: Any):
    target = _model_endpoint(problem, params.host_name)
    observer = _observer(problem, {params.host_name, params.host_name_2})
    if target is None or observer is None:
        return _result(problem, False, {"error": "no_independent_endpoint_path"})
    affected_ok = _endpoint_reachable(problem, observer, target)
    control = next(
        (
            item
            for item in problem.net_env.model.services
            if item.name not in {params.host_name, params.host_name_2}
        ),
        None,
    )
    control_ok = _endpoint_reachable(problem, observer, control) if control else None
    verified = affected_ok is False and control_ok is True
    return _result(
        problem,
        verified,
        {
            "observer": observer,
            "affected": target.name,
            "affected_ip": target.ip,
            "affected_ok": affected_ok,
            "control": control.name if control else None,
            "control_ok": control_ok,
        },
    )


def host_ip_conflict(problem: Any, params: Any):
    changed = _model_endpoint(problem, params.host_name_2)
    observer = _observer(problem, {params.host_name, params.host_name_2})
    if changed is None or observer is None:
        return _result(problem, False, {"error": "no_original_address_or_observer"})
    old_ip_ok = _endpoint_reachable(problem, observer, changed)
    control = next(
        (
            item
            for item in problem.net_env.model.services
            if item.name not in {params.host_name, params.host_name_2}
        ),
        None,
    )
    control_ok = _endpoint_reachable(problem, observer, control) if control else None
    verified = old_ip_ok is False and control_ok is True
    return _result(
        problem,
        verified,
        {
            "observer": observer,
            "changed_host": changed.name,
            "old_ip": changed.ip,
            "old_ip_ok": old_ip_ok,
            "control": control.name if control else None,
            "control_ok": control_ok,
        },
    )


def _switch_counters(problem: Any, switch: str) -> dict[str, Any]:
    raw = problem.runtime.exec(
        "fabric_mgr",
        f"python3 /opt/nika/p4rt_manager.py counters --switch {shlex.quote(switch)}",
        timeout=30,
    )
    return json.loads(raw).get("counters", {}).get(switch, {})


def tcp_syn_flood(problem: Any, params: Any):
    attacker = _model_endpoint(problem, params.attacker_device)
    if attacker is None:
        return _result(problem, False, {"error": "unknown_attacker_endpoint"})
    gateway = attacker.attached_switch
    before = _switch_counters(problem, gateway)
    time.sleep(2.0)
    after = _switch_counters(problem, gateway)

    def packets(counters: dict[str, Any], key: str) -> int:
        return int((counters.get(key, {}).get("0") or {}).get("packets", 0))

    syn_delta = packets(after, "flow_syn") - packets(before, "flow_syn")
    nonsyn_delta = packets(after, "flow_non_syn") - packets(before, "flow_non_syn")
    # The generator sends SYN-only packets at rate_pps; normal HTTP adds at
    # most a handful of SYNs during this two-second observation window.
    verified = syn_delta >= max(10, params.rate_pps // 4) and syn_delta > 3 * max(
        1, nonsyn_delta
    )
    return _result(
        problem,
        verified,
        {
            "gateway": gateway,
            "attacker": attacker.name,
            "target_ip": params.target_ip,
            "syn_packets_delta": syn_delta,
            "non_syn_packets_delta": nonsyn_delta,
            "window_sec": 2.0,
        },
    )


def lb_pending_connection_update(problem: Any, params: Any):
    """Observe the VIP selecting the replacement pool after the unsafe update."""
    model = problem.net_env.model
    source = model.client_on_gateway(params.host_name).name
    response = problem.runtime.exec(
        source,
        f"curl -fsS --connect-timeout 3 --max-time 8 {shlex.quote(model.vip_url)}",
        timeout=12,
    ).strip()
    control = problem.runtime.exec(
        source,
        f"curl -fsS --connect-timeout 3 --max-time 8 http://{model.backend_pool[0].ip}/",
        timeout=12,
    ).strip()
    replacement = model.backend_pool[1].name
    verified = response == replacement and control == model.backend_pool[0].name
    return _result(
        problem,
        verified,
        {
            "source": source,
            "vip_backend": response,
            "replacement_backend": replacement,
            "control_backend": control,
            "control_ok": control == model.backend_pool[0].name,
        },
    )


def int_insufficient_mtu_headroom(problem: Any, params: Any):
    """Compare telemetry for paired small and near-MTU UDP packets."""
    model = problem.net_env.model
    source = model.client_on_gateway(params.host_name)
    destination = model.services[0]
    switch_id = model.switch_info[params.host_name].device_id
    first_port = 31000 + (time.monotonic_ns() % 10000)
    script = (
        "import socket\n"
        f"target=({destination.ip!r},19409)\n"
        f"start={first_port}\n"
        "for index in range(24):\n"
        " for offset,size in ((0,32),(1,1450)):\n"
        "  sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
        "  sock.bind(('0.0.0.0',start+2*index+offset))\n"
        "  sock.sendto(bytes(size),target)\n"
        "  sock.close()\n"
    )
    problem.runtime.exec(source.name, f"python3 -c {shlex.quote(script)}", timeout=20)
    time.sleep(2.0)
    raw = problem.runtime.exec(
        "collector",
        "tail -n 20000 /var/lib/nika/int_reports.jsonl 2>/dev/null || true",
        timeout=20,
    )
    small_ports: set[int] = set()
    large_ports: set[int] = set()
    for line in raw.splitlines():
        try:
            report = json.loads(line)
            port = int(report.get("src_port", -1))
            hops = report.get("hop_sequence") or []
            selected = any(
                int(hop.get("switch_id", -1)) == switch_id
                and int(hop.get("egress_port", -1)) == params.bmv2_port
                for hop in hops
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if (
            report.get("src") != source.ip
            or report.get("dst") != destination.ip
            or not selected
        ):
            continue
        if first_port <= port < first_port + 48:
            index, offset = divmod(port - first_port, 2)
            (small_ports if offset == 0 else large_ports).add(index)
    missing_large = small_ports - large_ports
    verified = len(small_ports) >= 1 and len(missing_large) >= 1
    return _result(
        problem,
        verified,
        {
            "source": source.name,
            "destination": destination.name,
            "switch": params.host_name,
            "egress_port": params.bmv2_port,
            "small_int_flows": sorted(small_ports),
            "large_int_flows": sorted(large_ports),
            "small_without_large": sorted(missing_large),
        },
    )


def p4_ecn_threshold(problem: Any, params: Any):
    """Send ECT traffic and inspect queue depth and CE in per-hop reports."""
    model = problem.net_env.model
    source = (
        model.client_on_gateway(params.host_name)
        if params.host_name in model.gateways
        else model.clients[0]
    )
    destination = model.services[0]
    source_port = 42000 + (time.monotonic_ns() % 10000)
    script = (
        "import socket\n"
        "sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
        "sock.setsockopt(socket.IPPROTO_IP,socket.IP_TOS,2)\n"
        f"sock.bind(('0.0.0.0',{source_port}))\n"
        f"target=({destination.ip!r},19410)\n"
        "for _ in range(1200): sock.sendto(bytes(1000),target)\n"
        "sock.close()\n"
    )
    problem.runtime.exec(source.name, f"python3 -c {shlex.quote(script)}", timeout=25)
    time.sleep(2.0)
    raw = problem.runtime.exec(
        "collector",
        "tail -n 30000 /var/lib/nika/int_reports.jsonl 2>/dev/null || true",
        timeout=20,
    )
    switch_id = model.switch_info[params.host_name].device_id
    observed: list[dict[str, int]] = []
    for line in raw.splitlines():
        try:
            report = json.loads(line)
            if (
                report.get("src") != source.ip
                or report.get("dst") != destination.ip
                or int(report.get("src_port", -1)) != source_port
            ):
                continue
            for hop in report.get("hop_sequence") or []:
                if (
                    int(hop.get("switch_id", -1)) == switch_id
                    and int(hop.get("egress_port", -1)) == params.bmv2_port
                ):
                    observed.append(
                        {
                            "depth": int(hop.get("queue_occupancy", 0)),
                            "ecn": int(hop.get("ecn", -1)),
                        }
                    )
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    queued = [sample for sample in observed if sample["depth"] >= 32]
    # A threshold of 1024 should leave queued ECT traffic unmarked where the
    # healthy 32-packet threshold would have marked CE.
    verified = bool(queued) and all(sample["ecn"] != 3 for sample in queued)
    return _result(
        problem,
        verified,
        {
            "source": source.name,
            "destination": destination.name,
            "switch": params.host_name,
            "egress_port": params.bmv2_port,
            "observed_packets": len(observed),
            "queued_packets": len(queued),
            "max_queue_depth": max(
                (sample["depth"] for sample in observed), default=None
            ),
            "ce_queued_packets": sum(sample["ecn"] == 3 for sample in queued),
        },
    )


def icmp_frag_needed_filter(problem: Any, params: Any):
    """Observe real type 3/code 4 packets enter the gateway and disappear."""
    model = problem.net_env.model
    source = model.client_on_gateway(params.host_name)
    destination = model.services[0]

    def totals() -> tuple[int, int]:
        counters = _switch_counters(problem, params.host_name)

        def packets(direction: str) -> int:
            return sum(
                int(value.get("packets", 0))
                for value in counters.get(direction, {}).values()
            )

        return packets("ingress"), packets("egress")

    before = totals()
    command = (
        f"hping3 -1 -C 3 -K 4 -c 12 -i u20000 {shlex.quote(destination.ip)} "
        ">/tmp/nika-audit-frag-needed.log 2>&1 || true"
    )
    problem.runtime.exec(source.name, command, timeout=15)
    after = totals()
    ingress_delta = after[0] - before[0]
    egress_delta = after[1] - before[1]
    # The packet is forwarded by the ordinary LPM path without the ACL;
    # counters therefore distinguish a terminal P4 drop from a mere write ack.
    verified = ingress_delta >= 10 and egress_delta < ingress_delta // 2
    return _result(
        problem,
        verified,
        {
            "source": source.name,
            "destination": destination.name,
            "gateway": params.host_name,
            "ingress_packets_delta": ingress_delta,
            "egress_packets_delta": egress_delta,
            "send_log": problem.runtime.exec(
                source.name, "tail -n 5 /tmp/nika-audit-frag-needed.log", timeout=10
            )[:500],
        },
    )
