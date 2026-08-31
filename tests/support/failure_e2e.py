"""Shared harness for parametrized failure inject → verify → symptom → recover E2E."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.problems.registry import get_problem_class
from tests.support.failure_e2e_hooks import HOOKS, FailureE2EContext
from tests.support.symptom import evaluate_symptom


@dataclass(frozen=True)
class FailureE2ECase:
    problem: str
    scenario: str
    env_run_args: tuple[str, ...] = ()
    topo_size: str = "s"
    inject_params: dict[str, str] | None = None
    inject_seed: int = 1
    isp_options: dict[str, str] | None = None
    checks: frozenset[str] = frozenset({"verify", "symptom", "recover"})
    param_overrides: dict[str, str] = field(default_factory=dict)
    sleep_after_inject_sec: float = 0.0


def _resolve_params(case: FailureE2ECase) -> dict[str, str]:
    if case.inject_params is not None:
        params = dict(case.inject_params)
    else:
        params = resolve_inject_params(
            case.problem,
            case.scenario,
            case.topo_size,
            seed=case.inject_seed,
            isp_options=case.isp_options,
        )
    if case.param_overrides:
        params.update(case.param_overrides)
    return params


def run_failure_e2e(
    case: FailureE2ECase,
    *,
    scenario_kwargs: dict[str, Any],
    stage_seconds: dict[str, float] | None = None,
    stage_statuses: dict[str, str] | None = None,
) -> dict[str, float]:
    """Run inject/verify/symptom/recover for one failure case inside an active session."""
    timings = stage_seconds if stage_seconds is not None else {}

    def record(stage: str, started: float) -> None:
        timings[stage] = round(timings.get(stage, 0.0) + time.monotonic() - started, 3)

    params = _resolve_params(case)
    cls = get_problem_class(case.problem)
    assert cls is not None
    problem = cls(scenario_name=case.scenario, **scenario_kwargs)
    parsed = problem.parse_params(params)
    runtime = problem.runtime
    hooks = HOOKS.get(case.problem, {})

    ctx = FailureE2EContext(
        problem_name=case.problem,
        scenario=case.scenario,
        topo_size=case.topo_size,
        problem=problem,
        parsed=parsed,
        runtime=runtime,
    )

    if "pre_inject" in hooks:
        started = time.monotonic()
        try:
            hooks["pre_inject"](ctx)
        finally:
            record("pre_inject", started)

    recovered_ok = False
    try:
        started = time.monotonic()
        try:
            problem.inject_fault(parsed)
        finally:
            record("inject", started)
        if case.sleep_after_inject_sec:
            time.sleep(case.sleep_after_inject_sec)

        if "verify" in case.checks:
            started = time.monotonic()
            try:
                verify = problem.verify_fault(parsed)
            finally:
                record("verify", started)
            assert verify["verified"] is True, verify
            ctx.verify = verify
        if stage_statuses is not None:
            stage_statuses["inject"] = "pass"

        if "symptom" in case.checks:
            eval_kwargs: dict[str, Any] = {
                "scenario": case.scenario,
                "topo_size": case.topo_size,
                "problem": problem,
            }
            if ctx.before is not None and hasattr(ctx.before, "ping_ok"):
                eval_kwargs["before"] = ctx.before
            started = time.monotonic()
            try:
                ok, symptom = evaluate_symptom(
                    runtime,
                    case.problem,
                    parsed,
                    **eval_kwargs,
                )
            finally:
                record("symptom", started)
            assert ok is True, symptom
            ctx.symptom = symptom

        if "post_inject" in hooks:
            started = time.monotonic()
            try:
                hooks["post_inject"](ctx)
            finally:
                record("post_inject", started)

        if "recover" in case.checks:
            if not hasattr(problem, "recover_fault"):
                raise AssertionError(
                    f"{case.problem} does not implement recover_fault; "
                    "omit 'recover' from checks"
                )
            started = time.monotonic()
            try:
                recovered = problem.recover_fault(parsed)
            finally:
                record("recover", started)
            recovered_ok = recovered["verified"] is True
            assert recovered["verified"] is True, recovered
            ctx.recovered = recovered
            if case.problem == "load_balancer_overload":
                assert recovered["details"]["load_gone"] is True
            if "post_recover" in hooks:
                started = time.monotonic()
                try:
                    hooks["post_recover"](ctx)
                finally:
                    record("post_recover", started)
        if stage_statuses is not None:
            stage_statuses["symptom"] = "pass"
    finally:
        recover = getattr(problem, "recover_fault", None)
        if recover is not None and not recovered_ok:
            started = time.monotonic()
            try:
                recover(parsed)
            except Exception:  # noqa: BLE001
                pass
            finally:
                record("cleanup_recover", started)
    return timings
