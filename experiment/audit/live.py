"""Full environment audit for one concrete case.

Benchmark runs do not call this module. It deploys a lab and measures, on the
same lab:

1. the healthy lab and a same-metric healthy reading of the symptom signal;
2. a negative control: the symptom predicate evaluated on the healthy lab;
3. the injected artifact and the symptom after inject, with a control path;
4. the artifact and the symptom again after ``PERSISTENCE_WAIT_SEC`` and after
   a further ``FINAL_WAIT_SEC``.

It also runs the stages in ``experiment.audit.agent_view``:
what an agent's default tools show on the healthy lab, and a scan of the state
inject added for artifacts that name NIKA or the fault.

It closes only the session it started.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from experiment.audit.environment import (
    CaseAudit,
    StageResult,
    classify_observation,
    identity_from_row,
)
from experiment.audit.provenance import capture_provenance
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
            stage = _stage(name, "fail", f"{type(exc).__name__}: {exc}"[:500])
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
    # A destination inside a prefix the fault withdraws or hijacks is the fault.
    nets = []
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


def audit_open_session(session_id: str, row: dict[str, Any]) -> CaseAudit:
    """Audit a case whose lab is already deployed. Does not close the session."""
    from nika.net_env.isp.identity import is_isp_scenario
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.utils.session import Session

    identity = identity_from_row(row)
    fault = identity.fault
    probe = "" if is_healthy_case(fault) else declared_probe(fault)
    session = Session().load_running_session(session_id=session_id)
    kwargs = dict(getattr(session, "scenario_params", None) or {})
    # Session metadata records the fixed ISP scale for benchmark sampling.
    # The scenario id owns the topology, and the lab constructor rejects topo_size.
    if is_isp_scenario(session.scenario_name):
        kwargs.pop("topo_size", None)
        kwargs.pop("topo", None)
    net_env = get_net_env_instance(session.scenario_name, **kwargs)
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
        from experiment.audit.agent_view import run_agent_view

        return _agent_view_stage(
            name,
            lambda: run_agent_view(
                session_id, net_env, identity.scenario, identity.topo_size
            ),
        )

    if is_healthy_case(fault):
        clock = time.monotonic()
        stages.append(view("agent_view"))
        # The agent view runs inside the wait, so final_lab keeps its offset.
        time.sleep(
            max(0.0, PERSISTENCE_WAIT_SEC + FINAL_WAIT_SEC - (time.monotonic() - clock))
        )
        stages.append(_timed("final_lab", lambda: _lab_stage("final_lab", net_env)))
        return done()

    cls = get_problem_class(fault)
    if cls is None or probe == "undeclared":
        stages.append(_stage("baseline_path", "unsupported", f"no symptom for {fault}"))
        return done()

    problem = cls(scenario_name=session.scenario_name, **kwargs)
    parsed = problem.parse_params(
        {key: str(value) for key, value in identity.inject.items()}
    )
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
        before = run_probe_snapshot(runtime, probe, path, params=parsed)
        return _baseline_path(probe, before)

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
    scan: Any = None
    scan_error = "the pre-inject snapshot did not run"
    from experiment.audit.agent_view import leak_scan_for

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
        problem.inject_fault(parsed)
        verify = problem.verify_fault(parsed)
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


def audit_case(row: dict[str, Any]) -> CaseAudit:
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
        audit = audit_open_session(session_id, row)
        if audit.provenance is not None:
            audit.provenance.completed_at = datetime.now(UTC).isoformat()
        return audit
    finally:
        close_session(session_id, undeploy=True, status="finished")
