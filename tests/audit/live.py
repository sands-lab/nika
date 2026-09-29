"""Full environment audit for one concrete case.

Benchmark runs do not call this module. It deploys a lab, measures the healthy
baseline, injects the fault, and repeats artifact and symptom reads across a
short window. It closes only the session it started.
"""

from __future__ import annotations

import time
from typing import Any

from nika.audit.environment import (
    CaseAudit,
    StageResult,
    classify_observation,
    identity_from_row,
)
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.healthy import is_healthy_case
from tests.support.failure_e2e_hooks import HOOKS, FailureE2EContext
from tests.support.scenario_evaluate import evaluate_scenario
from tests.support.symptom.contracts import get_symptom_contract, list_symptom_contracts
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


def _control_stage(fault: str, payload: dict[str, Any] | None) -> StageResult:
    body = payload if isinstance(payload, dict) else {}
    after = body.get("after") if isinstance(body.get("after"), dict) else {}
    if "control_ok" in body or "control_ok" in after:
        value = body.get("control_ok", after.get("control_ok"))
        if value is True:
            return _stage("control_path", "pass", evidence={"fault": fault})
        if value is False:
            return _stage(
                "control_path", "fail", "control path failed", {"fault": fault}
            )
        return _stage(
            "control_path", "no_evidence", "missing observation", {"fault": fault}
        )
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
    net_env = get_net_env_instance(session.scenario_name, **kwargs)
    stages: list[StageResult] = []
    pause = window_for(fault) if window_sec is None else window_sec

    ok, lab = _lab(net_env)
    stages.append(_from_observation("baseline_lab", lab, ok=ok))

    if is_healthy_case(fault):
        time.sleep(pause)
        ok, lab = _lab(net_env)
        stages.append(_from_observation("final_lab", lab, ok=ok))
        return CaseAudit(identity=identity, stages=stages, symptom_probe=probe)

    cls = get_problem_class(fault)
    if cls is None:
        stages.append(_stage("inject_artifact", "fail", f"unknown fault {fault}"))
        return CaseAudit(identity=identity, stages=stages, symptom_probe=probe)

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

    if probe == "artifact_only":
        stages.append(_stage("baseline_path", "no_evidence", "artifact_only"))
    elif probe in {"custom", "undeclared"}:
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

    symptom_ok, symptom = evaluate_symptom(
        runtime,
        fault,
        parsed,
        scenario=identity.scenario,
        topo_size=identity.topo_size or "s",
        before=ctx.before,
        problem=problem,
    )
    symptom_payload = symptom if isinstance(symptom, dict) else {}
    stages.append(_from_observation("symptom", symptom_payload, ok=symptom_ok))
    stages.append(_control_stage(fault, symptom_payload))

    time.sleep(pause)
    stages.append(
        _artifact_stage("persistence_artifact", problem.recheck_artifact(parsed))
    )
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

    stages.append(_artifact_stage("final_artifact", problem.recheck_artifact(parsed)))
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
    return CaseAudit(identity=identity, stages=stages, symptom_probe=probe)


def audit_case(row: dict[str, Any], *, window_sec: float | None = None) -> CaseAudit:
    """Deploy ``row``, audit it, and undeploy that session."""
    from nika.utils.session_id import resolve_session_tag
    from nika.workflows.env.start import start_net_env
    from nika.workflows.session.close import close_session

    size = row.get("topo_size") or None
    kwargs: dict[str, Any] = {}
    for key in ("topo", "igp", "bgp_mode", "rpki", "backend", "device_profile"):
        if key in row and row[key] is not None:
            kwargs[key] = row[key]
    session_id = start_net_env(
        str(row["scenario"]),
        size,
        session_tag=resolve_session_tag(context="test"),
        **kwargs,
    )
    try:
        return audit_open_session(session_id, row, window_sec=window_sec)
    finally:
        close_session(session_id, undeploy=True, status="finished")
