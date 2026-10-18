"""Unified test-path ``evaluate_symptom`` for every registered failure."""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import compare_symptom
from nika.validation.symptom.types import ProbeSnapshot
from nika.runtime.base import LabRuntime
from nika.validation.symptom.contracts import get_symptom_contract
from nika.validation.symptom.custom import evaluate_custom_symptom
from nika.validation.symptom.probe import (
    _resolve_path,
    run_probe_snapshot,
    symptom_class_to_expect,
)


def evaluate_symptom(
    runtime: LabRuntime,
    failure: str,
    params: Any,
    *,
    scenario: str | None,
    topo_size: str = "s",
    before: ProbeSnapshot | None = None,
    problem: Any | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Confirm expected network impact after inject (tests only).

    Uses the failure's symptom contract ``probe``. Failures with
    ``probe="custom"`` require a live ``problem`` instance and dispatch to
    ``nika.validation.symptom.custom``.
    """
    contract = get_symptom_contract(failure)
    if contract is None:
        return False, {"error": "undeclared_symptom", "failure": failure}
    if contract.probe == "custom":
        if problem is None:
            return False, {
                "error": "custom_requires_problem_instance",
                "symptom_class": contract.symptom_class,
            }
        return evaluate_custom_symptom(failure, problem, params)

    path = _resolve_path(scenario, params, topo_size=topo_size)
    if path is None:
        return False, {"error": "no_probe_path", "scenario": scenario}
    after = run_probe_snapshot(runtime, contract.probe, path, params=params)
    before_snap = before if before is not None else ProbeSnapshot()
    if after.extra.get("error") or before_snap.extra.get("error"):
        return False, {
            "error": after.extra.get("error") or before_snap.extra.get("error"),
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
        }
    if contract.probe == "dns_answer":
        before_answers = before_snap.extra.get("dns_answers")
        after_answers = after.extra.get("dns_answers")
        ok = bool(before_answers and after_answers and before_answers != after_answers)
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "client_dns_answer_changed",
                "observed": ok,
            },
        }
    if after.extra.get("error"):
        return False, {"error": after.extra["error"], "after": after.as_dict()}
    expect = symptom_class_to_expect(contract.symptom_class)
    if contract.symptom_class == "gray":
        expect = "gray_loss"

    ok, cmp_details = compare_symptom(
        before_snap.as_dict(),
        after.as_dict(),
        expect,  # type: ignore[arg-type]
        loss_min_percent=contract.loss_min_percent,
        latency_factor=contract.latency_factor,
    )
    return ok, {
        "failure": failure,
        "probe": contract.probe,
        "symptom_class": contract.symptom_class,
        "before": before_snap.as_dict(),
        "after": after.as_dict(),
        "comparison": cmp_details,
    }
