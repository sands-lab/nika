"""Symptom checks on a case image, the VM snapshot that every trial restores.

Untimed case images carry injected faults; timed images carry a healthy
network and start their timeline after restore. Every symptom baseline is
read on the healthy network before saving either kind of image.
:func:`capture_baseline` reads it in the lab host process and keeps it in
memory; the snapshot saves that memory with the lab. After a restore,
:func:`evaluate_on_snapshot` compares the restored task against it on the
failure instance the snapshot carries.
"""

from __future__ import annotations

import json
from typing import Any

from nika.validation.presence import bound_injected_problem
from nika.validation.symptom.contracts import get_symptom_contract

# Baselines read before injection, keyed by the session of the case image build.
_BASELINES: dict[str, dict[str, Any]] = {}
_TASKS: dict[str, dict[str, Any]] = {}


def _key(fault: str, inject: dict[str, Any]) -> str:
    return json.dumps(
        [fault, {str(k): str(v) for k, v in inject.items()}], sort_keys=True
    )


# Problem attributes the symptom baselines set for the post-inject check.
_BASELINE_ATTR_PREFIX = "_audit_"


def _topo_size(problem: Any) -> str:
    return str(getattr(problem.net_env, "topo_size", "") or "s")


def capture_baseline(
    session_id: str, fault: str, inject: dict[str, Any]
) -> dict[str, Any]:
    """Read ``fault``'s healthy symptom baseline before it is injected."""
    from nika.utils.session import Session
    from nika.validation.symptom.custom import evaluate_custom_baseline
    from nika.validation.symptom.probe import _resolve_path, run_probe_snapshot
    from nika.workflows.failure.inject import prepare_failure

    contract = get_symptom_contract(fault)
    if contract is None:
        return {"status": "unsupported", "reason": "undeclared symptom"}
    session = Session().load_running_session(session_id=session_id)
    prepared = prepare_failure(session, [fault], inject)
    if session_id not in _TASKS:
        _TASKS[session_id] = {
            "scenario": session.scenario_name,
            "problem": fault,
            "inject": inject,
        }
    problem, params = prepared.problem, prepared.params
    record: dict[str, Any] = {"before": None, "attrs": {}}
    if contract.probe == "custom":
        own = evaluate_custom_baseline(fault, problem, params)
        if own is None:
            return {"status": "unsupported", "reason": "no custom baseline"}
        ok, evidence = own
        record["attrs"] = {
            key: value
            for key, value in vars(problem).items()
            if key.startswith(_BASELINE_ATTR_PREFIX)
        }
    else:
        path = _resolve_path(
            problem.scenario_name,
            params,
            topo_size=_topo_size(problem),
            net_env=problem.net_env,
        )
        if path is None:
            return {"status": "no_evidence", "reason": "no probe path"}
        record["before"] = run_probe_snapshot(
            problem.runtime, contract.probe, path, params=params
        )
        ok, evidence = True, record["before"].as_dict()
    _BASELINES.setdefault(session_id, {})[_key(fault, inject)] = record
    return {"status": "pass" if ok else "fail", "evidence": evidence}


def capture_task_baseline(session_id: str, row: dict[str, Any]) -> dict[str, Any]:
    """Read every member's baseline on the healthy environment."""
    from nika.schedule import schedule_for_task

    _TASKS[session_id] = row
    results = {}
    for event in schedule_for_task(row).events:
        if event.problem:
            results[_key(event.problem, event.inject)] = capture_baseline(
                session_id, event.problem, event.inject
            )
    return {
        "members": results,
        "ok": bool(results) and all(r["status"] == "pass" for r in results.values()),
    }


def evaluate_on_snapshot(session_id: str, baseline_session: str) -> dict[str, Any]:
    """Observe members and the declared relation on the restored task."""
    from nika.validation.presence import recheck_artifact_with_retry
    from nika.problems.multi import MultiFaultProblem, fault_instance_keys
    from nika.problems.combinations import combination_for_row, task_placement
    from nika.schedule import schedule_for_task
    from nika.validation.symptom.evaluate import evaluate_symptom
    from nika.validation.symptom.combinations import CHECKS, ProbeContext
    from nika.workflows.schedule import scheduler_for

    row = _TASKS.get(baseline_session)
    records = _BASELINES.get(baseline_session, {})
    if row is None or not records:
        return {
            "ok": False,
            "payload": {"error": "the case image has no task baseline"},
        }
    plan = schedule_for_task(row)
    scheduler = scheduler_for(session_id)
    bound = bound_injected_problem(session_id)
    members = []
    if scheduler is not None and not plan.static:
        for key, prepared in scheduler._prepared.items():
            if key in scheduler._active:
                event = plan.events[key[0]]
                members.append(
                    (event.problem, event.inject, prepared.problem, prepared.params)
                )
    elif bound is not None:
        problem, params = bound
        if isinstance(problem, MultiFaultProblem):
            events = [event for event in plan.events if event.problem]
            for (label, member), event in zip(
                problem._fault_pairs(), events, strict=True
            ):
                members.append(
                    (event.problem, event.inject, member, params.sub_params[label])
                )
        else:
            event = next(e for e in plan.events if e.problem)
            members.append((event.problem, event.inject, problem, params))
    if not members:
        return {
            "ok": False,
            "payload": {"error": "no active injected failure is bound"},
        }
    results = {}
    for index, (fault, inject, problem, params) in enumerate(members):
        record = records.get(_key(fault, inject))
        if record is None:
            return {"ok": False, "payload": {"error": f"no baseline for {fault}"}}
        for key, value in record["attrs"].items():
            setattr(problem, key, value)
        ok, payload = evaluate_symptom(
            problem.runtime,
            fault,
            params,
            scenario=problem.scenario_name,
            topo_size=_topo_size(problem),
            before=record["before"],
            problem=problem,
        )
        results[f"{index}:{fault}"] = {
            "observed": bool(ok),
            "evidence": payload,
            "artifact": recheck_artifact_with_retry(problem, params),
        }
    if len(members) == 1 and len([e for e in plan.events if e.problem]) == 1:
        result = next(iter(results.values()))
        return {
            "ok": result["observed"] and bool(result["artifact"].get("present")),
            "payload": result["evidence"],
        }
    name = combination_for_row(row)
    if name is None:
        return {
            "ok": False,
            "payload": {
                "unsupported": True,
                "error": "no declared combination relation",
                "members": results,
            },
        }
    check = CHECKS[name]
    context = ProbeContext(
        runtime=members[0][2].runtime,
        net_env=members[0][2].net_env,
        inject=task_placement(row),
        problems=dict(
            zip(
                fault_instance_keys([fault for fault, _, _, _ in members]),
                [problem for _, _, problem, _ in members],
                strict=True,
            )
        ),
    )
    if check.probe is not None:
        observed, relation = check.probe(context)
    else:
        observed = next(iter(results.values()))["observed"]
        relation = {"masked_symptom_observed": observed}
    expected = check.mode != "masked"
    # A masked symptom must disappear with both faults present. Latent
    # members of joint outages can have no standalone user-facing symptom.
    valid = all(
        r["artifact"].get("present")
        and not any(
            (r["evidence"] or {}).get(key)
            for key in ("error", "skipped", "unsupported")
        )
        for r in results.values()
    )
    return {
        "ok": valid and bool(observed) == expected,
        "payload": {
            "members": results,
            "relation": {
                "combination": name,
                "mode": check.mode,
                "observed": bool(observed),
                "expected": expected,
                "evidence": relation,
            },
        },
    }
