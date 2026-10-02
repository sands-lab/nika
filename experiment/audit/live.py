"""Full environment audit for one concrete case.

Benchmark runs do not call this module. It deploys a lab, measures the healthy
baseline, injects the fault, and repeats artifact and symptom reads across a
short window. It closes only the session it started.
"""

from __future__ import annotations

import ipaddress
import time
from datetime import UTC, datetime
from typing import Any

from experiment.audit.environment import (
    CaseAudit,
    StageResult,
    classify_observation,
    identity_from_row,
)
from experiment.audit.provenance import capture_provenance
from nika.net_env.verify import (
    http_download_stats,
    http_ok,
    iperf_tcp_metrics,
    ping_ok,
    ping_stats,
)
from nika.problems.rca.inventory import (
    canonical_link_name,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.problems.registry import get_problem_class
from nika.validation.presence import recheck_artifact_with_retry
from nika.workflows.benchmark.healthy import is_healthy_case
from tests.support.failure_e2e_hooks import HOOKS, FailureE2EContext
from tests.support.scenario_evaluate import evaluate_scenario
from tests.support.symptom.contracts import get_symptom_contract, list_symptom_contracts
from tests.support.symptom.custom import evaluate_custom_baseline
from tests.support.symptom.evaluate import evaluate_symptom
from tests.support.symptom.probe import _resolve_path, run_probe_snapshot

# Seconds to leave a dynamic fault in place before the persistence read.
_DYNAMIC_WINDOW_SEC = {
    "link_flap": 5.0,
    "tcp_syn_flood_attack": 3.0,
    "web_dos_attack": 3.0,
    "load_balancer_overload": 3.0,
    "incast_traffic_network_limitation": 3.0,
    "sender_resource_contention": 3.0,
    "receiver_resource_contention": 3.0,
    "arp_cache_poisoning": 3.0,
}


def window_for(fault: str) -> float:
    """Return the persistence window for ``fault``."""
    return _DYNAMIC_WINDOW_SEC.get(fault, 2.0)


def declared_probe(fault: str) -> str:
    """Return the symptom-contract probe, or ``undeclared``."""
    declared = {item.failure: item.probe for item in list_symptom_contracts()}
    return str(declared.get(fault, "undeclared"))


def _stage(
    stage: str, status: str, reason: str | None = None, evidence: dict | None = None
) -> StageResult:
    return StageResult(
        stage=stage,
        status=status,  # type: ignore[arg-type]
        reason=reason,
        evidence=evidence or {},
    )


def _from_observation(
    stage: str, payload: dict[str, Any] | None, *, ok: bool | None
) -> StageResult:
    status, reason = classify_observation(payload, ok=ok)
    evidence = payload if isinstance(payload, dict) else {}
    return _stage(stage, status, reason, evidence)


def _baseline_path(probe: str, snapshot: Any) -> StageResult:
    data = snapshot.as_dict() if hasattr(snapshot, "as_dict") else {}
    if probe == "artifact_only":
        return _stage("baseline_path", "no_evidence", "artifact_only", data)
    if probe == "dns_answer":
        return _stage(
            "baseline_path",
            "pass" if data.get("dns_answers") else "fail",
            None
            if data.get("dns_answers")
            else "DNS name did not resolve before inject",
            data,
        )
    if probe == "bgp_hijack_route":
        absent = (
            data.get("bgp_query_ok") is True and data.get("bgp_target_present") is False
        )
        return _stage(
            "baseline_path",
            "pass" if absent else "fail",
            None if absent else "hijacked prefix was already present or unreadable",
            data,
        )
    if probe in {"custom", "undeclared"}:
        return _stage(
            "baseline_path",
            "unsupported",
            f"{probe} probe has no shared healthy baseline",
            data,
        )
    signals = [
        value
        for value in (
            getattr(snapshot, "ping_ok", None),
            getattr(snapshot, "http_ok", None),
            getattr(snapshot, "control_plane_ok", None),
            getattr(snapshot, "symptom_ok", None),
            getattr(snapshot, "control_ok", None),
        )
        if value is not None
    ]
    if not signals:
        return _stage("baseline_path", "no_evidence", "missing observation", data)
    if all(signals):
        return _stage("baseline_path", "pass", evidence=data)
    return _stage(
        "baseline_path",
        "fail",
        "target path was not healthy before inject",
        data,
    )


def _node(endpoint: str) -> str:
    return parse_endpoint(endpoint)[0] or ""


def _independent_control_ip(
    problem: Any, parsed: Any, runtime: Any, source: str, dst_ip: str
) -> tuple[str, str] | None:
    """Return a control ``(source, destination)`` that does not depend on the root cause.

    A source attached only to a root-cause node, or an attacker or load host
    named in the inject parameters, is replaced by an endpoint on an
    unaffected device. The probe destination is kept unless it is the control
    source itself or an endpoint reached only through a root-cause interface or
    node. It is then replaced by a neighbor on the same device or segment, else
    by an endpoint on an unaffected device. ``None`` means no independent
    control exists.
    """
    if getattr(problem, "net_env", None) is None:
        return source, dst_ip
    try:
        resources = problem.root_cause_resources(parsed)
    except Exception:  # noqa: BLE001 - faults without resolvable resources
        resources = []
    kinds = [(str(getattr(r.kind, "value", r.kind)), r.node, r.name) for r in resources]
    dead_nodes = {n for kind, n, _ in kinds if kind == "node" and n}
    links = [
        [str(ep) for ep in tps]
        for _key, tps in iter_link_termination_points(problem.net_env)
    ]
    faulted = {f"{n}:{name}" for kind, n, name in kinds if kind == "interface"}
    faulted |= {ep for eps in links for ep in eps if _node(ep) in dead_nodes}
    dead_links = {name for kind, _, name in kinds if kind == "link"}
    faulted |= {
        ep
        for eps in links
        if canonical_link_name(tuple(eps)) in dead_links
        for ep in eps
    }
    link_count: dict[str, int] = {}
    for eps in links:
        for ep in eps:
            link_count[_node(ep)] = link_count.get(_node(ep), 0) + 1
    endpoints = {n for n, count in link_count.items() if count == 1}
    endpoints |= set(getattr(problem.net_env, "hosts", None) or [])
    for members in (getattr(problem.net_env, "servers", None) or {}).values():
        endpoints |= set(members or [])

    behind = {ep for ep in faulted if _node(ep) in endpoints}
    # Attackers and load generators named in the inject parameters carry the fault.
    values = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
    actors = {
        item.strip()
        for key, value in (values.items() if isinstance(values, dict) else [])
        if "attacker" in key or key.startswith("load_")
        for entry in (value if isinstance(value, list) else [value])
        if isinstance(entry, str)
        for item in entry.split(",")
        if item.strip() in endpoints
    }
    behind |= {ep for eps in links for ep in eps if _node(ep) in actors}
    siblings: list[tuple[set[str], list[str]]] = []
    for eps in links:
        hit = [ep for ep in eps if ep in faulted]
        if not hit:
            continue
        if len(eps) == 2:
            behind |= {ep for ep in eps if ep not in hit and _node(ep) in endpoints}
            device = next(
                (_node(ep) for ep in hit if _node(ep) not in endpoints), _node(hit[0])
            )
            ports = [
                ep
                for other in links
                if len(other) == 2 and device in map(_node, other)
                for ep in other
                if _node(ep) != device and _node(ep) in endpoints
            ]
            siblings.append((False, set(map(_node, eps)), ports))
        else:
            siblings.append((True, set(map(_node, eps)), list(eps)))
    behind_nodes = {_node(ep) for ep in behind}
    peers = {ep: far for eps in links if len(eps) == 2 for ep, far in (eps, eps[::-1])}
    addresses: dict[str, str | None] = {}

    def lookup(endpoint: str, with_prefix: bool = False) -> str | None:
        try:
            node, intf = parse_endpoint(endpoint)
            return runtime.get_host_ip(node, intf, with_prefix=with_prefix)
        except RuntimeError:
            return None

    def address(endpoint: str) -> str | None:
        # A root-cause host may be too starved or broken to answer `ip addr`, and
        # router OSes may keep addresses outside the kernel. On a /30 or /31 the
        # far side's prefix still names this end.
        if endpoint not in addresses:
            found = lookup(endpoint)
            far = (
                None
                if found or endpoint not in peers
                else lookup(peers[endpoint], True)
            )
            if far and "/" in far:
                iface = ipaddress.ip_interface(far)
                net = iface.network
                pair = list(net) if net.prefixlen == 31 else list(net.hosts())
                if net.prefixlen >= 30:
                    found = next((str(ip) for ip in pair if ip != iface.ip), None)
            addresses[endpoint] = found
        return addresses[endpoint]

    unaffected = sorted(
        ep
        for eps in links
        if len(eps) == 2 and not set(eps) & faulted
        for ep in eps
        if _node(ep) in endpoints
    )
    if source in behind_nodes or source in dead_nodes:
        source = next(
            (
                _node(ep)
                for ep in unaffected
                if _node(ep) not in behind_nodes | dead_nodes
                and address(ep) not in (None, dst_ip)
            ),
            "",
        )
        if not source:
            return None
    attached = {_node(ep) for eps in links if source in map(_node, eps) for ep in eps}
    # From the far end of a faulted link, the device's other ports are normally
    # reached across that same link. Segment members only count from inside it.
    neighbors = [
        ep
        for segment, link_nodes, ports in siblings
        if (source in link_nodes if segment else not attached & link_nodes)
        for ep in ports
    ]
    own = [ep for eps in links for ep in eps if _node(ep) == source]
    if not any(address(ep) in (None, dst_ip) for ep in behind):
        if not any(address(ep) == dst_ip for ep in own):
            return source, dst_ip
        neighbors += [ep for eps in links if set(own) & set(eps) for ep in eps]
    neighbors += unaffected
    # Address the neighbor on the interface that shares the device or segment.
    for endpoint in dict.fromkeys(neighbors):
        candidate, intf = parse_endpoint(endpoint)
        if not candidate or not intf or candidate in behind_nodes:
            continue
        if candidate == source or candidate in dead_nodes:
            continue
        candidate_ip = address(endpoint)
        if candidate_ip and candidate_ip != dst_ip:
            return source, candidate_ip
    return None


def _note_sibling_control(
    runtime: Any,
    scenario: str,
    parsed: Any,
    topo_size: str,
    payload: dict[str, Any],
    problem: Any,
) -> dict[str, Any]:
    """Record a declared control's outcome, including a failed observation."""
    after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
    details = payload.get("details")
    controls = (
        payload.get("control_ok"),
        after.get("control_ok"),
        details.get("control_ok") if isinstance(details, dict) else None,
    )
    if any(value is False for value in controls):
        return {**payload, "control_ok": False}
    if any(value is True for value in controls):
        return {**payload, "control_ok": True}
    path = _resolve_path(scenario, parsed, topo_size=topo_size)
    if path is None or not path.peer_host or path.peer_host == path.src_host:
        return payload
    ok: bool | None = None
    source = path.peer_host
    dst_ip = path.dst_ip
    if dst_ip:
        control = _independent_control_ip(problem, parsed, runtime, source, dst_ip)
        if control is None:
            return payload
        source, dst_ip = control
    url = None
    # The gateway VIP answers TCP/80 and does not answer ICMP.
    if dst_ip == "20.0.0.1" and path.http_url:
        url = path.http_url
        ok = http_ok(runtime, source, url)
    elif dst_ip:
        ok = ping_ok(runtime, source, dst_ip)
    elif path.http_url:
        url = path.http_url
        ok = http_ok(runtime, source, url)
    if ok is None:
        return payload
    noted = dict(payload)
    noted["control_ok"] = ok
    noted["control_path"] = {
        "source": source,
        "destination_ip": dst_ip,
        "http_url": url,
    }
    return noted


def _healthy_custom_baseline(
    runtime: Any, scenario: str, parsed: Any, topo_size: str
) -> StageResult | None:
    """Return a passing baseline when the scenario path is healthy."""
    path = _resolve_path(scenario, parsed, topo_size=topo_size)
    if path is None:
        return None
    if path.http_url:
        kind = "path_http"
    elif path.dst_ip:
        kind = "path_ping"
    else:
        return None
    snapshot = run_probe_snapshot(runtime, kind, path, params=parsed)
    stage = _baseline_path(kind, snapshot)
    if stage.status != "pass":
        return None
    return stage


def _corruption_baseline(
    problem: Any, parsed: Any, scenario: str, size: str
) -> dict[str, Any]:
    """Capture the same TCP path used by the corruption symptom probe."""
    from tests.support.symptom.corruption_probes import (
        IPERF_DURATION_SEC,
        _resolve_iperf_dst_ip,
        _service_peer_host,
    )

    path = _resolve_path(scenario, parsed, topo_size=size)
    if path is None or not path.dst_ip:
        return {"tcp_baseline_error": "no_probe_path"}
    peer = (
        _service_peer_host(problem, path, scenario)
        if problem.root_cause_name == "device_forwarding_packet_corruption"
        else path.peer_host or path.src_host
    )
    peer_ip = _resolve_iperf_dst_ip(problem.runtime, peer, path.dst_ip)
    bps, retrans = iperf_tcp_metrics(
        problem.runtime,
        path.src_host,
        peer,
        peer_ip,
        duration_sec=IPERF_DURATION_SEC,
        port=15221,
    )
    problem._baseline_iperf_bps = bps
    problem._baseline_iperf_retrans = retrans
    ping = ping_stats(
        problem.runtime, path.src_host, path.dst_ip, count=20, interval_sec=0.05
    )
    problem._baseline_rtt_ms = ping.rtt_avg_ms
    evidence: dict[str, Any] = {
        "baseline_iperf_bps": bps,
        "baseline_iperf_retrans": retrans,
        "baseline_ping_rtt_ms": ping.rtt_avg_ms,
        "baseline_ping_loss_percent": ping.loss_percent,
        "tcp_peer": peer,
    }
    if path.http_url:
        http = http_download_stats(
            problem.runtime,
            path.src_host,
            path.http_url,
            max_time_sec=30,
            connect_timeout_sec=5,
        )
        problem._baseline_http_time_s = http.time_total_s if http.ok else None
        evidence["baseline_http_time_s"] = problem._baseline_http_time_s
    return evidence


def _control_stage(fault: str, payload: dict[str, Any] | None) -> StageResult:
    body = payload if isinstance(payload, dict) else {}
    after = body.get("after") if isinstance(body.get("after"), dict) else {}
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    controls = (
        body.get("control_ok"),
        after.get("control_ok"),
        details.get("control_ok"),
    )
    evidence = {"fault": fault}
    if isinstance(body.get("control_path"), dict):
        evidence["path"] = body["control_path"]
    if any(value is False for value in controls):
        return _stage("control_path", "fail", "control path failed", evidence)
    if any(value is True for value in controls):
        return _stage("control_path", "pass", evidence=evidence)
    # Probe snapshots always include the key. None means this fault has
    # no separate control path, which is different from a failed probe.
    return _stage(
        "control_path",
        "unsupported",
        "no_control_path",
        {"fault": fault, "scope": fault},
    )


def _artifact_stage(stage: str, raw: dict[str, Any]) -> StageResult:
    present = bool(raw.get("present"))
    return _stage(
        stage,
        "pass" if present else "fail",
        None if present else str(raw.get("error") or "fault artifact absent"),
        raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {},
    )


def _lab(net_env: Any) -> tuple[bool, dict[str, Any]]:
    ok, result = evaluate_scenario(net_env)
    if not isinstance(result, dict):
        return ok, {}
    return ok, result


def _scenario_effect(
    net_env: Any,
    healthy_checks: dict[str, Any],
    *,
    expected: set[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Observe a persistent regression in scenario network-health checks."""
    _, current = _lab(net_env)
    checks = current.get("checks") if isinstance(current.get("checks"), dict) else {}
    regressed = {
        name
        for name, healthy in healthy_checks.items()
        if healthy is True and checks.get(name) is False
    }
    observed = regressed if expected is None else regressed & expected
    ok = bool(observed)
    return ok, {
        "verified": ok,
        "probe": "scenario_health_delta",
        "regressed_checks": sorted(regressed),
        "persistent_checks": sorted(observed),
        "expected_checks": sorted(expected) if expected is not None else None,
        "checks": checks,
    }


def audit_open_session(
    session_id: str,
    row: dict[str, Any],
    *,
    window_sec: float | None = None,
) -> CaseAudit:
    """Audit a case whose lab is already deployed. Does not close the session."""
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.utils.session import Session

    identity = identity_from_row(row)
    fault = identity.fault
    probe = declared_probe(fault) if not is_healthy_case(fault) else ""
    session = Session().load_running_session(session_id=session_id)
    kwargs = dict(getattr(session, "scenario_params", None) or {})
    from nika.net_env.isp.identity import is_isp_scenario

    # Session metadata records the fixed ISP scale for benchmark sampling.
    # The scenario id owns the topology, and the lab constructor rejects topo_size.
    if is_isp_scenario(session.scenario_name):
        kwargs.pop("topo_size", None)
        kwargs.pop("topo", None)
    net_env = get_net_env_instance(session.scenario_name, **kwargs)
    provenance = capture_provenance(session_id, net_env._build_runtime())
    stages: list[StageResult] = []
    pause = window_for(fault) if window_sec is None else window_sec

    ok, lab = _lab(net_env)
    stages.append(_from_observation("baseline_lab", lab, ok=ok))

    if is_healthy_case(fault):
        time.sleep(pause)
        ok, lab = _lab(net_env)
        stages.append(_from_observation("final_lab", lab, ok=ok))
        return CaseAudit(
            identity=identity,
            stages=stages,
            symptom_probe=probe,
            provenance=provenance,
        )

    cls = get_problem_class(fault)
    if cls is None:
        stages.append(_stage("inject_artifact", "fail", f"unknown fault {fault}"))
        return CaseAudit(
            identity=identity,
            stages=stages,
            symptom_probe=probe,
            provenance=provenance,
        )

    problem = cls(scenario_name=session.scenario_name, **kwargs)
    parsed = problem.parse_params(
        {key: str(value) for key, value in identity.inject.items()}
    )
    runtime = problem.runtime
    contract = get_symptom_contract(fault) if probe not in {"", "undeclared"} else None
    hooks = HOOKS.get(fault, {})
    ctx = FailureE2EContext(
        problem_name=fault,
        scenario=identity.scenario,
        topo_size=identity.topo_size or "s",
        problem=problem,
        parsed=parsed,
        runtime=runtime,
    )
    if "pre_inject" in hooks:
        hooks["pre_inject"](ctx)

    healthy_checks = lab.get("checks") if isinstance(lab.get("checks"), dict) else {}
    if probe == "artifact_only":
        stages.append(
            _stage(
                "baseline_path",
                "pass" if ok and healthy_checks else "no_evidence",
                None if ok and healthy_checks else "missing healthy scenario checks",
                {"checks": healthy_checks},
            )
        )
    elif probe in {"custom", "undeclared"}:
        own = evaluate_custom_baseline(fault, problem, parsed)
        healthy = (
            _healthy_custom_baseline(
                runtime,
                identity.scenario,
                parsed,
                identity.topo_size or "s",
            )
            if own is None
            else None
        )
        if own is not None:
            stages.append(
                _stage(
                    "baseline_path",
                    "pass" if own[0] else "fail",
                    None if own[0] else "target path was not healthy before inject",
                    own[1],
                )
            )
        elif healthy is not None:
            stages.append(healthy)
        else:
            stages.append(
                _stage(
                    "baseline_path",
                    "unsupported",
                    f"{probe} probe has no shared healthy baseline",
                    {"scope": fault},
                )
            )
    else:
        path = _resolve_path(
            identity.scenario, parsed, topo_size=identity.topo_size or "s"
        )
        if path is None:
            stages.append(_stage("baseline_path", "no_evidence", "no_probe_path"))
        else:
            kind = contract.probe if contract is not None else probe
            snapshot = run_probe_snapshot(runtime, kind, path, params=parsed)
            stages.append(_baseline_path(probe, snapshot))
            if ctx.before is None:
                ctx.before = snapshot

    if fault in {"link_packet_corruption", "device_forwarding_packet_corruption"}:
        stages[-1].evidence.update(
            _corruption_baseline(
                problem, parsed, identity.scenario, identity.topo_size or "s"
            )
        )

    problem.inject_fault(parsed)
    verify = problem.verify_fault(parsed)
    verify_ok = bool(isinstance(verify, dict) and verify.get("verified"))
    details = verify.get("details") if isinstance(verify, dict) else {}
    if not isinstance(details, dict):
        details = {"details": details}
    stages.append(
        _stage(
            "inject_artifact",
            "pass" if verify_ok else "fail",
            None if verify_ok else "verify_fault reported the artifact absent",
            details,
        )
    )

    if probe == "artifact_only":
        symptom_ok, symptom = _scenario_effect(net_env, healthy_checks)
        effect_checks = set(symptom["persistent_checks"])
    else:
        symptom_ok, symptom = evaluate_symptom(
            runtime,
            fault,
            parsed,
            scenario=identity.scenario,
            topo_size=identity.topo_size or "s",
            before=ctx.before,
            problem=problem,
        )
        effect_checks = set()
    symptom_payload = symptom if isinstance(symptom, dict) else {}
    symptom_payload = _note_sibling_control(
        runtime,
        identity.scenario,
        parsed,
        identity.topo_size or "s",
        symptom_payload,
        problem,
    )
    stages.append(_from_observation("symptom", symptom_payload, ok=symptom_ok))
    stages.append(_control_stage(fault, symptom_payload))

    time.sleep(pause)
    stages.append(
        _artifact_stage(
            "persistence_artifact", recheck_artifact_with_retry(problem, parsed)
        )
    )
    if probe == "artifact_only":
        held_ok, held = _scenario_effect(
            net_env, healthy_checks, expected=effect_checks
        )
    else:
        held_ok, held = evaluate_symptom(
            runtime,
            fault,
            parsed,
            scenario=identity.scenario,
            topo_size=identity.topo_size or "s",
            before=ctx.before,
            problem=problem,
        )
    stages.append(
        _from_observation(
            "persistence_symptom", held if isinstance(held, dict) else {}, ok=held_ok
        )
    )

    stages.append(
        _artifact_stage("final_artifact", recheck_artifact_with_retry(problem, parsed))
    )
    if probe == "artifact_only":
        final_ok, final = _scenario_effect(
            net_env, healthy_checks, expected=effect_checks
        )
    else:
        final_ok, final = evaluate_symptom(
            runtime,
            fault,
            parsed,
            scenario=identity.scenario,
            topo_size=identity.topo_size or "s",
            before=ctx.before,
            problem=problem,
        )
    stages.append(
        _from_observation(
            "final_symptom", final if isinstance(final, dict) else {}, ok=final_ok
        )
    )
    return CaseAudit(
        identity=identity,
        stages=stages,
        symptom_probe=probe,
        provenance=provenance,
    )


def audit_case(row: dict[str, Any], *, window_sec: float | None = None) -> CaseAudit:
    """Deploy ``row``, audit it, and undeploy that session."""
    from nika.net_env.net_env_pool import scenario_requires_topo_size
    from nika.utils.session_id import resolve_session_tag
    from nika.workflows.env.start import start_net_env
    from nika.workflows.session.close import close_session

    scenario = str(row["scenario"])
    size = row.get("topo_size") or None
    if size and not scenario_requires_topo_size(scenario):
        size = None
    kwargs: dict[str, Any] = {}
    for key in ("topo", "igp", "bgp_mode", "rpki", "backend", "device_profile"):
        if key in row and row[key] is not None:
            kwargs[key] = row[key]
    session_id = start_net_env(
        scenario,
        size,
        session_tag=resolve_session_tag(context="test"),
        **kwargs,
    )
    try:
        audit = audit_open_session(session_id, row, window_sec=window_sec)
        if audit.provenance is not None:
            audit.provenance.completed_at = datetime.now(UTC).isoformat()
        return audit
    finally:
        close_session(session_id, undeploy=True, status="finished")
