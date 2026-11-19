"""Full environment audit for one concrete case.

Benchmark runs do not call this module. It deploys a lab and measures, on the
same lab:

1. the healthy lab and a same-metric healthy reading of the symptom signal;
2. a negative control: the symptom predicate evaluated on the healthy lab;
3. the injected artifact and the symptom after inject, with a control path;
4. the artifact and the symptom again after ``PERSISTENCE_WAIT_SEC`` and after
   a further ``FINAL_WAIT_SEC``.

It also runs the stages in ``scripts.benchmark_audit.agent_view``:
what an agent's default tools show on the healthy lab, and a scan of the state
inject added for artifacts that name NIKA or the fault.

It closes only the session it started.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.benchmark_audit.environment import (
    CaseAudit,
    StageResult,
    classify_observation,
    identity_from_row,
)
from scripts.benchmark_audit.provenance import AuditProvenance, capture_provenance
from nika.workflows.benchmark.release import read_git_commit
from nika.net_env.verify import (
    LabVerifyTimeoutError,
    http_ok,
    ping_ok,
    verify_lab_with_retry,
)
from nika.problems.rca.inventory import (
    canonical_link_name,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.problems.registry import get_problem_class
from nika.runtime.shared.execution import collect_exec_failures
from nika.validation.presence import recheck_artifact_with_retry
from nika.validation.symptom.contracts import get_symptom_contract
from nika.validation.symptom.custom import evaluate_custom_baseline
from nika.validation.symptom.evaluate import evaluate_symptom
from nika.validation.symptom.probe import _resolve_path, run_probe_snapshot
from nika.workflows.benchmark.healthy import is_healthy_case

# The persistence read comes after the OSPF dead interval (40 s) and BGP hold
# timers used in the labs, so a fault that only perturbs a protocol briefly does
# not persist. The final read comes one more window later.
PERSISTENCE_WAIT_SEC = 60.0
FINAL_WAIT_SEC = 60.0
# Stages whose pass means "the symptom is present". A probe command that timed
# out or raised there reads as a broken path, so it fails the stage instead.
_SYMPTOM_STAGES = frozenset({"symptom", "persistence_symptom", "final_symptom"})


def declared_probe(fault: str) -> str:
    """Return the symptom probe the fault class declares, or ``undeclared``."""
    if "+" in fault:
        return "combination"
    contract = get_symptom_contract(fault)
    return contract.probe if contract is not None else "undeclared"


def _stage(
    stage: str, status: str, reason: str | None = None, evidence: dict | None = None
) -> StageResult:
    return StageResult(
        stage=stage,
        status=status,  # type: ignore[arg-type]
        reason=reason,
        evidence=evidence or {},
    )


def _timed(name: str, measure: Callable[[], StageResult]) -> StageResult:
    """Run one stage, stamp its time, and fail a symptom read on command errors."""
    started = datetime.now(UTC).isoformat()
    clock = time.monotonic()
    with collect_exec_failures() as failures:
        try:
            stage = measure()
        except Exception as exc:  # noqa: BLE001 - one stage records its own error
            import traceback

            stage = _stage(
                name,
                "fail",
                f"{type(exc).__name__}: {exc}"[:500],
                {"traceback": "".join(traceback.format_exception(exc))[-8000:]},
            )
    stage.started_at = started
    stage.duration_sec = round(time.monotonic() - clock, 1)
    if failures:
        stage.evidence["exec_failures"] = failures[:20]
        if name in _SYMPTOM_STAGES and stage.status == "pass":
            stage.status = "fail"
            stage.reason = "a probe command timed out or raised"
    return stage


def _from_observation(
    stage: str, payload: dict[str, Any] | None, *, ok: bool | None
) -> StageResult:
    status, reason = classify_observation(payload, ok=ok)
    evidence = payload if isinstance(payload, dict) else {}
    return _stage(stage, status, reason, evidence)


def _baseline_path(probe: str, snapshot: Any) -> StageResult:
    data = snapshot.as_dict() if hasattr(snapshot, "as_dict") else {}
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
    from nika.runtime.spec import NodeRole
    from nika.telemetry import is_telemetry_link

    identities = getattr(problem.net_env, "machine_identities", {})
    management_nodes = {
        node
        for node, identity in identities.items()
        if identity.role in {NodeRole.CONTROLLER, NodeRole.INFRASTRUCTURE}
        and not identity.reachability_target
    }
    links = [
        [str(ep) for ep in tps]
        for key, tps in iter_link_termination_points(problem.net_env)
        if not is_telemetry_link(key)
        and not any(_node(ep) in management_nodes for ep in tps)
    ]
    intent = getattr(problem.net_env, "probe_intent", lambda: None)()
    isolated = intent.isolation_matcher() if intent else lambda _a, _b: False
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
    # A destination inside a prefix the fault withdraws or hijacks is the fault.
    nets = []
    declared_prefixes = problem.affected_prefixes(parsed)
    for prefix in declared_prefixes:
        nets.append(ipaddress.ip_network(prefix, strict=False))
    for value in values.values() if isinstance(values, dict) else []:
        if isinstance(value, str) and "/" in value:
            try:
                nets.append(ipaddress.ip_network(value, strict=False))
            except ValueError:
                pass
    in_fault_prefix = any(
        ipaddress.ip_address(dst_ip) in net
        for net in nets
        if ipaddress.ip_address(dst_ip).version == net.version
    )
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
    # Exclude entire branches disconnected by the root cause. Their hosts can
    # also lose DHCP renewal or the return route to an otherwise healthy server.
    graph: dict[str, set[str]] = {}
    for eps in links:
        nodes = {_node(ep) for ep in eps if ep not in faulted} - dead_nodes
        for node in nodes:
            graph.setdefault(node, set()).update(nodes - {node})
    remaining = set(graph)
    components = []
    while remaining:
        component = set()
        pending = [min(remaining)]
        while pending:
            node = pending.pop()
            if node not in component:
                component.add(node)
                pending.extend(graph[node] - component)
        components.append(component)
        remaining -= component
    if components:
        intact = max(components, key=lambda nodes: (len(nodes), sorted(nodes)))
        behind_nodes |= set(graph) - intact
        behind |= {ep for eps in links for ep in eps if _node(ep) in behind_nodes}
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

    # A host inside a prefix the fault withdraws or hijacks loses its replies too.
    if nets:
        for ep in sorted({ep for eps in links for ep in eps if _node(ep) in endpoints}):
            ip = address(ep)
            if ip and any(
                ipaddress.ip_address(ip) in net
                for net in nets
                if ipaddress.ip_address(ip).version == net.version
            ):
                behind_nodes.add(_node(ep))
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
    if not in_fault_prefix and not any(address(ep) in (None, dst_ip) for ep in behind):
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
        identity = identities.get(candidate)
        if (
            identity is not None
            and identity.role
            in {NodeRole.SWITCH, NodeRole.CONTROLLER, NodeRole.INFRASTRUCTURE}
            and not identity.reachability_target
        ):
            continue
        if isolated(source, candidate):
            continue
        candidate_ip = address(endpoint)
        if (
            candidate_ip
            and candidate_ip != dst_ip
            and not any(
                ipaddress.ip_address(candidate_ip) in net
                for net in nets
                if ipaddress.ip_address(candidate_ip).version == net.version
            )
        ):
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
        return {**payload, "control_unavailable": "scenario declares no peer host"}
    ok: bool | None = None
    source = path.peer_host
    dst_ip = path.dst_ip
    if dst_ip:
        control = _independent_control_ip(problem, parsed, runtime, source, dst_ip)
        if control is None:
            return {
                **payload,
                "control_unavailable": "every peer path crosses the root cause",
            }
        source, dst_ip = control
    url = None
    # A destination the scenario serves over HTTP (a load-balancer VIP) need
    # not answer ICMP, so probe the service itself.
    if dst_ip and path.http_url and urlparse(path.http_url).hostname == dst_ip:
        url = path.http_url
        ok = http_ok(runtime, source, url)
    elif dst_ip:
        ok = ping_ok(runtime, source, dst_ip, count=3)
    elif path.http_url:
        url = path.http_url
        ok = http_ok(runtime, source, url)
    if ok is None:
        return {**payload, "control_unavailable": "no destination to probe"}
    noted = dict(payload)
    noted["control_ok"] = ok
    noted["control_path"] = {
        "source": source,
        "destination_ip": dst_ip,
        "http_url": url,
    }
    return noted


def _control_stage(fault: str, payload: dict[str, Any] | None) -> StageResult:
    body = payload if isinstance(payload, dict) else {}
    after = body.get("after") if isinstance(body.get("after"), dict) else {}
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    controls = (
        body.get("control_ok"),
        after.get("control_ok"),
        details.get("control_ok"),
    )
    evidence: dict[str, Any] = {"fault": fault}
    if isinstance(body.get("control_path"), dict):
        evidence["path"] = body["control_path"]
    if any(value is False for value in controls):
        return _stage("control_path", "fail", "control path failed", evidence)
    if any(value is True for value in controls):
        return _stage("control_path", "pass", evidence=evidence)
    unavailable = body.get("control_unavailable")
    if unavailable:
        # The lab has no path that avoids the root cause. That is a property of
        # the topology, recorded and reported, and it does not block admission.
        return _stage(
            "control_path",
            "unsupported",
            "no_control_path",
            {**evidence, "why": unavailable},
        )
    return _stage(
        "control_path",
        "unsupported",
        "control path was not measured",
        evidence,
    )


def _artifact_stage(stage: str, raw: dict[str, Any]) -> StageResult:
    present = bool(raw.get("present"))
    return _stage(
        stage,
        "pass" if present else "fail",
        None if present else str(raw.get("error") or "fault artifact absent"),
        raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {},
    )


def _lab_stage(name: str, net_env: Any) -> StageResult:
    """Verify the lab within the same retry window as production startup."""
    try:
        result = verify_lab_with_retry(net_env)
    except LabVerifyTimeoutError as exc:
        stage = _from_observation(name, exc.last_result, ok=False)
        stage.evidence = {
            **stage.evidence,
            "failed": sorted(exc.failed_checks),
            "max_wait_sec": exc.max_wait_sec,
        }
        return stage
    if result is None:
        return _stage(name, "unsupported", "no_verify_lab")
    checks = result.get("checks") or {}
    ok = bool(result.get("verified")) and bool(checks) and all(checks.values())
    return _from_observation(name, result, ok=ok)


def _sham_stage(observe: Callable[[], tuple[bool, dict[str, Any]]]) -> StageResult:
    """Evaluate the symptom predicate on the healthy lab. It must not hold."""
    ok, payload = observe()
    payload = payload if isinstance(payload, dict) else {}
    status, reason = classify_observation(payload, ok=None)
    if status in {"fail", "no_evidence", "unsupported", "skipped"} and payload.get(
        "error"
    ):
        return _stage("sham_symptom", status, reason, payload)
    if ok:
        return _stage(
            "sham_symptom",
            "fail",
            "the symptom predicate holds on the healthy lab",
            payload,
        )
    return _stage("sham_symptom", "pass", None, payload)


def _custom_baseline_stage(fault: str, problem: Any, parsed: Any) -> StageResult:
    own = evaluate_custom_baseline(fault, problem, parsed)
    if own is None:
        return _stage(
            "baseline_path",
            "unsupported",
            "the fault declares no same-metric healthy baseline",
            {"scope": fault},
        )
    ok, evidence = own
    return _stage(
        "baseline_path",
        "pass" if ok else "fail",
        None if ok else "the symptom signal was not healthy before inject",
        evidence,
    )


def _agent_view_stage(name: str, measure: Callable[[], dict[str, Any]]) -> StageResult:
    """Healthy agent view: fail on checks that failed in every round."""

    def stage() -> StageResult:
        try:
            evidence = measure()
        except Exception as exc:  # noqa: BLE001 - an audit-side error is not a case failure
            return _stage(name, "no_evidence", f"{type(exc).__name__}: {exc}"[:500])
        violated = int(evidence.get("violated_count") or 0)
        if violated:
            return _stage(
                name,
                "fail",
                f"{violated} check(s) an agent would read as a fault failed in every round",
                evidence,
            )
        if not any(evidence.get("checks", {}).values()):
            return _stage(name, "no_evidence", "no agent-view check ran", evidence)
        return _stage(name, "pass", evidence=evidence)

    return _timed(name, stage)


def _leak_stage(measure: Callable[[], dict[str, Any]]) -> StageResult:
    """Injection leak scan: fail on any fault-revealing artifact."""

    def stage() -> StageResult:
        try:
            evidence = measure()
        except Exception as exc:  # noqa: BLE001 - an audit-side error is not a case failure
            return _stage(
                "leak_scan", "no_evidence", f"{type(exc).__name__}: {exc}"[:500]
            )
        flagged = int(evidence.get("flagged_count") or 0)
        if flagged:
            return _stage(
                "leak_scan",
                "fail",
                f"inject left {flagged} artifact(s) that name NIKA or the fault, "
                "or copy pre-fault state",
                evidence,
            )
        if not evidence.get("nodes_scanned"):
            return _stage("leak_scan", "no_evidence", "no node was readable", evidence)
        return _stage("leak_scan", "pass", evidence=evidence)

    return _timed("leak_scan", stage)


def _run_oracle(
    session_id: str, planned: dict[str, dict[str, Any]] | None = None
) -> tuple[Path, dict[str, Any]]:
    from nika.agents.oracle.agent import ORACLE_FILENAME, OracleAgent
    from nika.agents.mcp_config import engine_gateway
    from nika.benchmark_engine.protocol import GatewayRequest

    agent = OracleAgent(session_id, stream_output=False, planned=planned)
    path = Path(agent.session_dir) / ORACLE_FILENAME
    # A planned run is a control: keep the task's own Oracle result in place.
    kept = path.read_bytes() if planned is not None and path.is_file() else None
    task = getattr(agent.session, "task_description", None) or ""
    if planned is None and not task:
        raise ValueError("the session has no prepared task description")
    try:
        with engine_gateway(session_id, agent.session_dir, GatewayRequest()):
            asyncio.run(agent.diagnose(task))
        result = json.loads(path.read_text())
    finally:
        if planned is not None:
            if kept is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(kept)

    return path, result


def _fault_summaries(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in fault.items() if key != "steps"}
        for fault in result.get("faults") or []
    ]


def _oracle_stage(
    session_id: str, expected_faults: set[str] | None = None
) -> StageResult:
    """Require the reference playbook to diagnose this lab through MCP."""

    def diagnose() -> StageResult:
        try:
            path, result = _run_oracle(session_id)
        except ValueError as exc:
            if str(exc) == "the session has no prepared task description":
                return _stage("oracle", "no_evidence", str(exc))
            raise
        faults = result.get("faults") or []
        solved = (
            result.get("solved") is True
            and bool(faults)
            and all(fault.get("confirmed") is True for fault in faults)
            and (
                expected_faults is None
                or {fault.get("fault_type") for fault in faults} == expected_faults
            )
        )
        return _stage(
            "oracle",
            "pass" if solved else "fail",
            None if solved else "Oracle did not confirm the task from live evidence",
            {"path": str(path), "solved": solved, "faults": _fault_summaries(result)},
        )

    return _timed("oracle", diagnose)


def _audit_combination(session_id, row, net_env, kwargs, stages, view):
    """Use the catalog's intervention matrix, then execute the authored timeline."""
    from nika.problems.combinations import (
        task_placement,
        check_multi_fault_task,
        combination_for_row,
        get_combination,
    )
    from nika.schedule import schedule_for_task
    from nika.workflows.schedule import SessionScheduler
    from scripts.benchmark_audit.agent_view import leak_scan_for
    from tests.support.combination_e2e import build_combination_matrix

    check_multi_fault_task(row)
    name = combination_for_row(row)
    if name is None:
        stages.append(
            _stage("combination_matrix", "unsupported", "No catalog relation check")
        )
        return
    combination = get_combination(name)
    evidence = {"combination": name}
    matrix = build_combination_matrix(combination, row, kwargs, evidence)
    placement = task_placement(row)
    stages.append(_sham_oracle_stage(session_id, name, {}, planned=placement))
    stages.append(view("baseline_agent_view"))

    def interventions():
        try:
            matrix.run()
        except Exception as exc:
            return _stage("combination_matrix", "fail", str(exc)[:500], evidence)
        return _stage("combination_matrix", "pass", evidence=evidence)

    result = _timed("combination_matrix", interventions)
    stages.append(result)
    if result.status != "pass":
        return

    scan = leak_scan_for(net_env, combination.failures)
    scan.capture_before()
    scheduler = SessionScheduler(
        session_id,
        schedule_for_task(row),
        traffic=row.get("traffic"),
        expected_root_causes=row.get("root_causes"),
    )
    try:

        def timeline():
            scheduler.prepare()
            scheduler.start()
            scheduler.wait_agent_start()
            return _stage(
                "timeline",
                "pass",
                evidence={
                    "schedule": scheduler.plan.case_value(),
                    **scheduler.status(),
                },
            )

        stages.append(_timed("timeline", timeline))
        stages.append(
            _timed(
                "inject_artifact",
                lambda: _artifact_stage(
                    "inject_artifact", scheduler.recheck_artifact()
                ),
            )
        )

        def leaks():
            return scan.capture_after_and_diff()

        stages.append(_leak_stage(leaks))

        def relation():
            if matrix.check.mode == "masked":
                matrix.expect_masked("scheduled", combination.failures[0], False)
            else:
                matrix.expect_outage("scheduled", True)
            return _stage(
                "symptom",
                "pass",
                evidence={
                    "mode": matrix.check.mode,
                    **{k: v for k, v in evidence.items() if k.startswith("scheduled")},
                },
            )

        stages.append(_timed("symptom", relation))
        active_faults = set(combination.failures)
        stages.append(_oracle_stage(session_id, active_faults))
        identity = identity_from_row(row)
        for fault, key in zip(
            combination.failures, combination.instance_keys, strict=True
        ):
            control = _neighbor_oracle_stage(
                session_id,
                fault,
                identity.model_copy(update={"inject": placement[key]}),
                kwargs,
                net_env,
                active_faults=active_faults,
            )
            control.stage = f"neighbor_oracle:{key}"
            stages.append(control)
    finally:

        def recover():
            scheduler.close()
            return _stage("recovery", "pass", evidence=scheduler.status())

        stages.append(_timed("recovery", recover))
    stages.append(_timed("final_lab", lambda: _lab_stage("final_lab", net_env)))


def _sham_oracle_stage(
    session_id: str,
    fault: str,
    inject: dict[str, Any],
    *,
    planned: dict[str, dict[str, Any]] | None = None,
) -> StageResult:
    """Run the fault's playbook on the healthy lab. It must not confirm."""

    def diagnose() -> StageResult:
        _path, result = _run_oracle(
            session_id, planned=planned if planned is not None else {fault: inject}
        )
        faults = _fault_summaries(result)
        evidence = {"faults": faults}
        expected = (
            {key.split("[", 1)[0] for key in planned}
            if planned is not None
            else {fault}
        )
        if (
            {item.get("fault_type") for item in faults} != expected
            or any(item.get("error") for item in faults)
            or len(faults) != (len(planned) if planned is not None else 1)
        ):
            return _stage(
                "sham_oracle", "no_evidence", "the playbook did not run", evidence
            )
        if any(item.get("confirmed") is True for item in faults):
            return _stage(
                "sham_oracle",
                "fail",
                "the playbook confirms the fault on the healthy lab",
                evidence,
            )
        return _stage("sham_oracle", "pass", None, evidence)

    return _timed("sham_oracle", diagnose)


def _confusable_plans(
    fault: str, identity: Any, scenario_params: dict[str, Any], net_env: Any
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Inject params of each look-alike failure, aimed where ``fault`` was injected.

    Each look-alike starts from its own target on this scenario and takes the
    task's inject fields it shares, so its playbook inspects the same place.
    """
    from nika.agents.oracle.plan import fault_plan
    from nika.problems.support.benchmark_targets import NoValidTargetError
    from nika.problems.support.router_ops import (
        target_nodes,
        unsupported_native_targets,
    )
    from nika.workflows.benchmark.inject_resolve import (
        resolve_inject_params,
        validate_benchmark_case,
    )

    isp_options = {
        key: getattr(identity, key) or scenario_params.get(key)
        for key in ("igp", "bgp_mode", "backend", "device_profile")
        if getattr(identity, key) or scenario_params.get(key)
    }
    if identity.rpki:
        isp_options["rpki"] = identity.rpki.lower() == "true"
    topo_size = identity.topo_size or str(getattr(net_env, "topo_size", "") or "")
    planned: dict[str, dict[str, Any]] = {}
    skipped: dict[str, str] = {}
    for other in get_problem_class(fault).CONFUSABLE_WITH:
        if not get_problem_class(other).is_compatible(
            identity.scenario,
            isp_options.get("device_profile"),
            backend=isp_options.get("backend"),
        ):
            skipped[other] = "Failure is not supported on this deployment."
            continue
        try:
            base = resolve_inject_params(
                other,
                identity.scenario,
                topo_size,
                isp_options=isp_options or None,
                net_env=net_env,
            )
        except NoValidTargetError as exc:
            skipped[other] = str(exc)
            continue
        fields = get_problem_class(other).Params.model_fields
        aimed = {
            **base,
            **{key: value for key, value in identity.inject.items() if key in fields},
        }
        # Device and link faults use different field names for the same
        # attachment. Aim both playbooks at that attachment, not two defaults.
        if "forwarding_device" in identity.inject and "host_name" in fields:
            aimed["host_name"] = identity.inject["forwarding_device"]
        if "host_name" in identity.inject and "forwarding_device" in fields:
            target = identity.inject["host_name"]
            port = identity.inject.get("intf_name")
            forwarding = set(net_env.routers or []) | set(net_env.switches or [])
            if target in forwarding:
                aimed["forwarding_device"] = target
            else:
                for _, ends in iter_link_termination_points(net_env):
                    if f"{target}:{port}" not in ends:
                        continue
                    for end in ends:
                        node, intf = parse_endpoint(str(end))
                        if node in forwarding:
                            aimed.update(forwarding_device=node, intf_name=intf)
                            break
        for params in (aimed, base) if aimed != base else (base,):
            unsupported = unsupported_native_targets(
                get_problem_class(other), net_env, target_nodes(params)
            )
            if unsupported:
                if params == base:
                    skipped[other] = (
                        f"Failure has no native implementation for {unsupported}."
                    )
                continue
            try:
                if params != base:
                    validate_benchmark_case(
                        identity.scenario,
                        other,
                        params,
                        topo_size,
                        isp_options=isp_options or None,
                        net_env=net_env,
                        # A look-alike can be latent under the task's workload.
                        combined=True,
                    )
                fault_plan(other, identity.scenario, scenario_params, params)
            except ValueError as exc:
                if params == base:
                    raise ValueError(f"Cannot plan look-alike {other}: {exc}") from exc
                # Shared parameter names need not denote a valid look-alike target.
                continue
            planned[other] = params
            break
    return planned, skipped


def _neighbor_oracle_stage(
    session_id: str,
    fault: str,
    identity: Any,
    scenario_params: dict[str, Any],
    net_env: Any,
    *,
    active_faults: set[str] | None = None,
) -> StageResult:
    """Run the look-alike failures' playbooks on this task. None may confirm."""

    def diagnose() -> StageResult:
        planned, skipped = _confusable_plans(fault, identity, scenario_params, net_env)
        planned = {
            name: params
            for name, params in planned.items()
            if name not in (active_faults or set())
        }
        evidence: dict[str, Any] = {"planned": planned, "skipped": skipped}
        if not planned:
            return _stage(
                "neighbor_oracle", "unsupported", "no_confusable_on_scenario", evidence
            )
        _path, result = _run_oracle(session_id, planned=planned)
        faults = _fault_summaries(result)
        evidence["faults"] = faults
        confirmed = sorted(
            str(item.get("fault_type")) for item in faults if item.get("confirmed")
        )
        if confirmed:
            return _stage(
                "neighbor_oracle",
                "fail",
                f"the playbooks of {confirmed} also confirm on this task",
                evidence,
            )
        if {item.get("fault_type") for item in faults} != set(planned) or any(
            item.get("error") for item in faults
        ):
            return _stage(
                "neighbor_oracle",
                "no_evidence",
                "not every look-alike playbook ran",
                evidence,
            )
        return _stage("neighbor_oracle", "pass", None, evidence)

    return _timed("neighbor_oracle", diagnose)


def _restore_infrastructure_identities(net_env: Any, session: Any) -> None:
    """Restore declared deployment infrastructure before validating its topology."""
    from nika.runtime.spec import MachineInventory, NodeRole

    recorded = (session.metadata or {}).get("machine_identities")
    if recorded is None:
        return
    names = (
        set(net_env.lab.machines)
        if net_env.lab is not None
        else {node.name for node in net_env.get_lab_spec().nodes}
    )
    for name, identity in MachineInventory.from_dict(recorded).nodes.items():
        if (
            identity.role is NodeRole.INFRASTRUCTURE
            and name in names
            and name not in net_env.machine_identities
        ):
            net_env.declare_machine(
                name,
                role=identity.role,
                capabilities=identity.capabilities,
                reachability_target=identity.reachability_target,
            )


def _session_net_env(session):
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.runtime.factory import net_env_kwargs_for_session, resolve_backend
    from nika.runtime.meta import meta_path

    net_env = get_net_env_instance(
        session.scenario_name,
        lab_name=session.lab_name,
        backend=resolve_backend(session),
        **net_env_kwargs_for_session(session),
    )
    net_env.topology_file = meta_path(session, "topology_file", scenario_params=True)
    net_env.runtime_workdir = meta_path(
        session, "runtime_workdir", scenario_params=True
    )
    return net_env


def audit_open_session(session_id: str, row: dict[str, Any]) -> CaseAudit:
    """Audit a case whose lab is already deployed. Does not close the session."""
    from nika.runtime.factory import resolve_backend
    from nika.utils.session import Session
    from nika.workflows.failure.inject import inject_prepared_failure, prepare_failure

    identity = identity_from_row(row)
    fault = identity.fault
    probe = "" if is_healthy_case(fault) else declared_probe(fault)
    session = Session().load_running_session(session_id=session_id)
    kwargs = dict(getattr(session, "scenario_params", None) or {})
    # The Oracle builds playbooks from the same parameters (see OracleAgent).
    oracle_params = dict(kwargs)
    oracle_params.setdefault("backend", resolve_backend(session))
    net_env = _session_net_env(session)
    _restore_infrastructure_identities(net_env, session)
    provenance = capture_provenance(session_id, net_env._build_runtime())
    stages: list[StageResult] = []

    def done() -> CaseAudit:
        return CaseAudit(
            identity=identity,
            stages=stages,
            symptom_probe=probe,
            provenance=provenance,
        )

    stages.append(_timed("baseline_lab", lambda: _lab_stage("baseline_lab", net_env)))

    def view(name: str) -> StageResult:
        from scripts.benchmark_audit.agent_view import run_agent_view

        return _agent_view_stage(
            name,
            lambda: run_agent_view(
                session_id, net_env, identity.scenario, identity.topo_size
            ),
        )

    if is_healthy_case(fault):
        from nika.workflows.benchmark.healthy import write_healthy_session_artifacts

        clock = time.monotonic()
        stages.append(view("agent_view"))
        write_healthy_session_artifacts(session_id)
        stages.append(_oracle_stage(session_id))
        # The agent view runs inside the wait, so final_lab keeps its offset.
        time.sleep(
            max(0.0, PERSISTENCE_WAIT_SEC + FINAL_WAIT_SEC - (time.monotonic() - clock))
        )
        stages.append(_timed("final_lab", lambda: _lab_stage("final_lab", net_env)))
        return done()

    if len(identity.problems) > 1:
        _audit_combination(session_id, row, net_env, kwargs, stages, view)
        return done()

    cls = get_problem_class(fault)
    if cls is None or probe == "undeclared":
        stages.append(_stage("baseline_path", "unsupported", f"no symptom for {fault}"))
        return done()

    prepared = prepare_failure(session, [fault], identity.inject)
    problem, parsed = prepared.problem, prepared.params
    runtime = problem.runtime
    size = identity.topo_size or "s"
    before: Any = None

    def baseline() -> StageResult:
        nonlocal before
        if probe == "custom":
            return _custom_baseline_stage(fault, problem, parsed)
        path = _resolve_path(identity.scenario, parsed, topo_size=size)
        if path is None:
            return _stage("baseline_path", "no_evidence", "no_probe_path")
        observations = []
        attempts = 3 if probe == "path_http" else 1
        for attempt in range(attempts):
            before = run_probe_snapshot(runtime, probe, path, params=parsed)
            stage = _baseline_path(probe, before)
            observations.append(before.as_dict())
            if stage.status == "pass" or attempt + 1 == attempts:
                break
            time.sleep(5)
        if len(observations) > 1:
            stage.evidence["baseline_attempts"] = observations
        return stage

    def observe() -> tuple[bool, dict[str, Any]]:
        ok, payload = evaluate_symptom(
            runtime,
            fault,
            parsed,
            scenario=identity.scenario,
            topo_size=size,
            before=before,
            problem=problem,
        )
        return ok, payload if isinstance(payload, dict) else {}

    stages.append(_timed("baseline_path", baseline))
    stages.append(_timed("sham_symptom", lambda: _sham_stage(observe)))
    stages.append(_sham_oracle_stage(session_id, fault, identity.inject))
    scan: Any = None
    scan_error = "the pre-inject snapshot did not run"
    from scripts.benchmark_audit.agent_view import leak_scan_for

    stages.append(view("baseline_agent_view"))
    try:
        scan = leak_scan_for(net_env, fault)
        scan.capture_before()
    except Exception as exc:  # noqa: BLE001 - recorded by the leak stage
        scan, scan_error = None, f"{type(exc).__name__}: {exc}"

    def leak() -> dict[str, Any]:
        if scan is None:
            raise RuntimeError(scan_error)
        return scan.capture_after_and_diff()

    def inject() -> StageResult:
        verify = inject_prepared_failure(
            session, [fault], prepared, param_overrides=identity.inject
        )
        verified = bool(isinstance(verify, dict) and verify.get("verified"))
        details = verify.get("details") if isinstance(verify, dict) else {}
        return _stage(
            "inject_artifact",
            "pass" if verified else "fail",
            None if verified else "verify_fault reported the artifact absent",
            details if isinstance(details, dict) else {"details": details},
        )

    stages.append(_timed("inject_artifact", inject))
    # Read right after inject, before the audit's own probes touch the lab.
    stages.append(_leak_stage(leak))
    symptom_payload: dict[str, Any] = {}

    def symptom() -> StageResult:
        nonlocal symptom_payload
        ok, payload = observe()
        symptom_payload = _note_sibling_control(
            runtime, identity.scenario, parsed, size, payload, problem
        )
        return _from_observation("symptom", symptom_payload, ok=ok)

    stages.append(_timed("symptom", symptom))
    stages.append(_control_stage(fault, symptom_payload))
    stages.append(_oracle_stage(session_id))
    stages.append(
        _neighbor_oracle_stage(session_id, fault, identity, oracle_params, net_env)
    )

    for label, wait in (
        ("persistence", PERSISTENCE_WAIT_SEC),
        ("final", FINAL_WAIT_SEC),
    ):
        time.sleep(wait)
        stages.append(
            _timed(
                f"{label}_artifact",
                lambda label=label: _artifact_stage(
                    f"{label}_artifact", recheck_artifact_with_retry(problem, parsed)
                ),
            )
        )

        def again(label: str = label) -> StageResult:
            ok, payload = observe()
            return _from_observation(f"{label}_symptom", payload, ok=ok)

        stages.append(_timed(f"{label}_symptom", again))
    return done()


def audit_case(row: dict[str, Any], *, release: str | None = None) -> CaseAudit:
    """Deploy ``row``, audit it, and undeploy that session.

    ``release`` deploys and injects with that release's environments and
    failure variants, as a benchmark run of the release does.
    """
    from nika.net_env.net_env_pool import scenario_requires_topo_size
    from nika.utils.session import Session
    from nika.utils.session_id import resolve_session_tag
    from nika.workflows.env.start import start_net_env
    from nika.workflows.session.close import close_session

    scenario = str(row["scenario"])
    size = row.get("topo_size") or None
    if size and not scenario_requires_topo_size(scenario):
        size = None
    kwargs: dict[str, Any] = {}
    for key in (
        "topo",
        "igp",
        "bgp_mode",
        "rpki",
        "backend",
        "device_profile",
        "traffic",
        "telemetry",
    ):
        if key in row and row[key] is not None:
            kwargs[key] = row[key]
    session_id = start_net_env(
        scenario,
        size,
        session_tag=resolve_session_tag(context="test"),
        release=release,
        **kwargs,
    )
    try:
        if release:
            Session().load_running_session(session_id=session_id).update_session(
                "benchmark_version", release
            )
        audit = audit_open_session(session_id, row)
        if audit.provenance is not None:
            audit.provenance.completed_at = datetime.now(UTC).isoformat()
        return audit
    finally:
        close_session(session_id, undeploy=True, status="finished")


def _image_results_dir() -> str:
    import os

    from nika.config import RUNTIME_DIR

    return str(
        Path(
            os.environ.get("NIKA_AUDIT_RESULTS_DIR")
            or RUNTIME_DIR / "environment-audit-results"
        )
    )


def _audit_healthy_image(row: dict[str, Any], *, release: str | None) -> CaseAudit:
    """Run the healthy audit inside the lab image restored for a healthy task."""
    import shlex

    from nika.firecracker.base import GUEST_REPO
    from nika.firecracker.image import variant_for_row
    from nika.firecracker.trial import TrialLab
    from nika.firecracker.vmm import VSOCK, guest_exec
    from nika.utils.session import Session
    from nika.utils.session_id import make_session_id, resolve_session_tag
    from nika.workflows.session.close import close_session

    sid = make_session_id(session_tag=resolve_session_tag(context="test"))
    started = datetime.now(UTC).isoformat()
    commit, dirty = read_git_commit()
    with TrialLab(
        variant_for_row(row),
        trial_key=f"audit-{sid}",
        owner=_image_results_dir(),
    ) as lab:
        sid = lab.attach(session_id=sid)
        try:
            session = Session().load_running_session(session_id=sid)
            record_path = f"/tmp/nika-healthy-audit-{sid}.json"
            # Kathara includes its owning Unix user in container identities.
            # The guest daemon and the deployed lab run as ubuntu.
            script = (
                "from pathlib import Path\n"
                "from scripts.benchmark_audit.live import audit_open_session\n"
                "from nika.utils.session import Session\n"
                "from nika.utils.logger import bind_session_dir\n"
                f"session = Session().load_running_session(session_id={sid!r})\n"
                f"session.update_session('benchmark_version', {release!r})\n"
                "bind_session_dir(session.session_dir)\n"
                f"audit = audit_open_session({sid!r}, {row!r})\n"
                f"Path({record_path!r}).write_text(audit.model_dump_json())\n"
            )
            command = "su ubuntu -c " + shlex.quote(
                f"cd {GUEST_REPO} && NIKA_REMOTE_SERVER=1 "
                "PYTHONPATH=src .venv/bin/python -"
            )
            output = guest_exec(lab.dir / VSOCK, command, stdin=script.encode())
            (Path(session.session_dir) / "healthy-image-audit.log").write_text(output)
            audit = CaseAudit.model_validate_json(
                guest_exec(lab.dir / VSOCK, f"cat {shlex.quote(record_path)}")
            )
            assert lab.image is not None
            audit.mode = "case_image"
            audit.provenance = AuditProvenance(
                git_commit=commit,
                git_dirty=dirty,
                started_at=started,
                completed_at=datetime.now(UTC).isoformat(),
                session_id=sid,
                images={
                    "firecracker": {
                        "reference": f"lab:{lab.image.key}",
                        "image_id": f"lab:{lab.image.key}",
                        "repo_digests": [],
                    }
                },
            )
            return audit
        finally:
            close_session(session_id=sid, undeploy=True, status="finished")


def audit_case_image(row: dict[str, Any], *, release: str | None = None) -> CaseAudit:
    """Audit ``row`` on its Firecracker case image, the state every trial restores.

    The image carries the injected failure and the healthy symptom baseline the
    lab host read before injection. The healthy-lab stages run in
    :func:`audit_case`; this checks that the restored task still shows its
    artifact and symptom and that the Oracle, but no look-alike, confirms it.
    """
    from nika.firecracker.image import variant_for_row
    from nika.firecracker.trial import TrialLab
    from nika.remote.workflows import _client
    from nika.runtime.factory import resolve_backend
    from nika.utils.session import Session
    from nika.utils.session_id import make_session_id, resolve_session_tag
    from nika.workflows.session.close import close_session

    if is_healthy_case(str(row.get("problem") or "")):
        return _audit_healthy_image(row, release=release)

    identity = identity_from_row(row)
    fault = identity.fault
    members = identity.problems or [fault]
    session_id = make_session_id(session_tag=resolve_session_tag(context="test"))
    stages: list[StageResult] = []

    def symptom(name: str, sid: str, baseline_session: str) -> StageResult:
        result = _client().symptom(sid, baseline_session=baseline_session)
        return _from_observation(name, result.get("payload"), ok=result.get("ok"))

    def artifact(name: str, sid: str) -> StageResult:
        check = _client().fault_artifact(sid)
        present = bool(check.get("present"))
        return _stage(
            name,
            "pass" if present else "fail",
            None if present else str(check.get("error") or "fault artifact absent"),
            dict(check.get("evidence") or {}),
        )

    with TrialLab(
        variant_for_row(row),
        trial_key=f"audit-{session_id}",
        owner=_image_results_dir(),
        case_row=row,
    ) as lab:
        assert lab.case is not None and lab.image is not None
        commit, dirty = read_git_commit()
        provenance = AuditProvenance(
            git_commit=commit,
            git_dirty=dirty,
            started_at=datetime.now(UTC).isoformat(),
            session_id=session_id,
            # The lab runs inside the VM: name its images instead of containers.
            images={
                "firecracker": {
                    "reference": f"case:{lab.case.key}",
                    "image_id": f"lab:{lab.image.key}",
                    "repo_digests": [],
                }
            },
        )
        # attach() fails unless the restored copy still carries the fault.
        sid = lab.attach(session_id=session_id)
        scheduler = None
        try:
            if release:
                Session().load_running_session(session_id=sid).update_session(
                    "benchmark_version", release
                )
            if not lab.case.injected:
                from nika.remote.schedule import RemoteSessionScheduler
                from nika.schedule import schedule_for_task

                scheduler = RemoteSessionScheduler(
                    sid,
                    schedule_for_task(row),
                    traffic=row.get("traffic"),
                    expected_root_causes=row.get("root_causes"),
                )

                def timeline():
                    scheduler.prepare()
                    scheduler.start()
                    scheduler.wait_agent_start()
                    # Final diagnosis needs every injection's records on the
                    # host, which the remote scheduler pulls on completion.
                    while not scheduler.complete.is_set():
                        scheduler.wait_for_progress(
                            scheduler.observation_revision(), timeout=1.0
                        )
                        scheduler.raise_if_failed()
                    return _stage("timeline", "pass", evidence=scheduler.status())

                stages.append(_timed("timeline", timeline))
            stages.append(
                _timed("image_artifact", lambda: artifact("image_artifact", sid))
            )
            stages[-1].evidence.update(case=lab.case.key, timings=lab.timings)
            baseline = lab.case.session_id
            stages.append(
                _timed("image_symptom", lambda: symptom("image_symptom", sid, baseline))
            )
            active_faults = set(members)
            stages.append(_oracle_stage(sid, active_faults))
            session = Session().load_running_session(session_id=sid)
            params = dict(getattr(session, "scenario_params", None) or {})
            oracle_params = {**params, "backend": resolve_backend(session)}
            net_env = _session_net_env(session)
            _restore_infrastructure_identities(net_env, session)
            from nika.problems.combinations import task_placement

            placement = task_placement(row) if len(identity.problems) > 1 else {}
            from nika.problems.multi import fault_instance_keys

            for member, key in zip(members, fault_instance_keys(members), strict=True):
                control = _neighbor_oracle_stage(
                    sid,
                    member,
                    identity.model_copy(update={"inject": placement[key]})
                    if placement
                    else identity,
                    oracle_params,
                    net_env,
                    active_faults=active_faults,
                )
                if len(identity.problems) > 1:
                    control.stage = f"neighbor_oracle:{key}"
                stages.append(control)
            for label, wait in (
                ("image_persistence", PERSISTENCE_WAIT_SEC),
                ("image_final", FINAL_WAIT_SEC),
            ):
                time.sleep(wait)
                stages.append(
                    _timed(
                        f"{label}_artifact",
                        lambda label=label: artifact(f"{label}_artifact", sid),
                    )
                )
                stages.append(
                    _timed(
                        f"{label}_symptom",
                        lambda label=label: symptom(f"{label}_symptom", sid, baseline),
                    )
                )
        finally:
            try:
                if scheduler is not None:
                    scheduler.close()
            finally:
                close_session(session_id=sid, undeploy=True, status="finished")
    provenance.completed_at = datetime.now(UTC).isoformat()
    return CaseAudit(
        identity=identity,
        stages=stages,
        symptom_probe=declared_probe(fault),
        provenance=provenance,
        mode="case_image",
    )
