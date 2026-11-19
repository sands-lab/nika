"""Live test of one tested multi-fault task from the catalog.

One session per task injects each failure alone, then together, recovers
each, and finally lets the Oracle diagnose both failures injected through the
normal workflow.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.problems.combinations import FailureCombination, task_placement
from nika.problems.registry import get_problem_class
from nika.problems.support.polling import wait_until
from nika.validation.symptom import evaluate_symptom
from nika.validation.symptom.custom import evaluate_custom_baseline
from nika.validation.symptom.combinations import CHECKS, ProbeContext
from tests.support.failure_e2e_hooks import FailureE2EContext

# Wait at least the OSPF dead interval (40 s) before reading the outage, and
# allow BGP over the overlay up to its hold time to converge.
SETTLE_SEC = 45
OUTAGE_TIMEOUT_SEC = 240


def _env_args(row: dict[str, Any]) -> list[str]:
    args = []
    if row.get("topo_size") and scenario_requires_topo_size(row["scenario"]):
        args += ["-s", row["topo_size"]]
    for key, flag in (
        ("igp", "--igp"),
        ("bgp_mode", "--bgp-mode"),
        ("backend", "--backend"),
        ("device_profile", "--device-profile"),
    ):
        if row.get(key):
            args += [flag, str(row[key])]
    if row.get("rpki"):
        args.append("--rpki")
    return args


def build_combination_matrix(combination, row, kwargs, evidence):
    """Prepare the catalog's checks against an existing healthy session."""
    check = CHECKS[combination.name]
    placement = task_placement(row)
    scenario = row["scenario"]
    topo_size = str(row.get("topo_size") or "")
    contexts: dict[str, FailureE2EContext] = {}
    for name, key in zip(combination.failures, combination.instance_keys, strict=True):
        problem = get_problem_class(name)(scenario_name=scenario, **kwargs)
        contexts[key] = FailureE2EContext(
            problem_name=name,
            scenario=scenario,
            topo_size=topo_size,
            problem=problem,
            parsed=problem.parse_params(placement[key]),
            runtime=problem.runtime,
        )
    for name, ctx in contexts.items():
        # Symptom evaluators compare against this healthy reading. A
        # failure may sit where it shows no symptom alone, so only masked
        # combinations, which judge failures by their own symptoms, need it.
        baseline = evaluate_custom_baseline(name, ctx.problem, ctx.parsed)
        evidence[f"baseline: {name}"] = baseline
        if check.mode == "masked" and baseline is not None:
            assert baseline[0] is True, (name, baseline[1])
    return _Matrix(combination, check, contexts, evidence)


def run_combination_e2e(
    test: Any, combination: FailureCombination, row: dict[str, Any]
) -> dict[str, Any]:
    """Run the matrix and the Oracle on one catalog task, then close the session."""
    placement = task_placement(row)
    scenario = row["scenario"]
    evidence: dict[str, Any] = {"placement": placement}
    session_id = test._start_env(scenario, _env_args(row))
    evidence["session_id"] = session_id
    try:
        test._assert_session_ready(session_id, scenario)
        kwargs = test._scenario_kwargs(session_id)
        matrix = build_combination_matrix(combination, row, kwargs, evidence)
        matrix.run()
        _oracle_solves_both(test, session_id, matrix, placement, row)
    finally:
        try:
            directory = Path(test._session_row(session_id)["session_dir"])
            (directory / "combination-evidence.json").write_text(
                json.dumps(evidence, indent=2) + "\n"
            )
        finally:
            test._close_session(session_id)
    return evidence


def _cli(args: list[str], *, timeout: int = 1800) -> str:
    result = subprocess.run(
        ["uv", "run", "nika", *args], capture_output=True, text=True, timeout=timeout
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _oracle_solves_both(test, session_id, matrix, placement, row) -> None:
    """Inject both failures through the normal workflow; the Oracle confirms each.

    A masked failure must still leave evidence the diagnosis tools can read,
    otherwise the task is not solvable.
    """
    combination = matrix.combination
    from nika.schedule import schedule_for_task
    from nika.workflows.schedule import SessionScheduler

    scheduler = SessionScheduler(session_id, schedule_for_task(row))
    try:
        scheduler.prepare()
        scheduler.start()
        scheduler.wait_agent_start()
        if matrix.check.mode == "masked":
            matrix.expect_masked("oracle", combination.failures[0], False)
        else:
            matrix.expect_outage("oracle", True)
        _cli(["agent", "run", "--agent", "oracle", "--session_id", session_id])
    finally:
        scheduler.close()
        matrix.evidence["schedule_recovery"] = scheduler.status()
    directory = Path(test._session_row(session_id)["session_dir"])
    oracle = json.loads((directory / "oracle.json").read_text())
    truth = json.loads((directory / "ground_truth.json").read_text())
    matrix.evidence["oracle"] = oracle
    assert {cause["fault_type"] for cause in truth["root_causes"]} == set(
        combination.failures
    ), truth
    assert oracle.get("solved") is True, oracle
    assert len(oracle["faults"]) == len(combination.failures), oracle
    from nika.problems.rca import canonical_root_causes

    submission = json.loads((directory / "submission.json").read_text())
    assert canonical_root_causes(submission["root_causes"]) == canonical_root_causes(
        truth["root_causes"]
    )


class _Matrix:
    def __init__(self, combination, check, contexts, evidence):
        self.combination = combination
        self.check = check
        self.contexts = contexts
        self.evidence = evidence
        self.active: list[str] = []

    def inject(self, name: str) -> None:
        ctx = self.contexts[name]
        ctx.problem.inject_fault(ctx.parsed)
        self.active.append(name)
        result = ctx.problem.verify_fault(ctx.parsed)
        self.evidence.setdefault("artifacts", []).append({"failure": name, **result})
        assert result["verified"], (name, result)

    def recover(self, name: str) -> None:
        """Restore and verify this member's artifact while others may be active."""
        from nika.problems.support.recovery import artifact_recovery

        ctx = self.contexts[name]
        with artifact_recovery():
            result = ctx.problem.recover_fault(ctx.parsed)
        self.evidence.setdefault("recoveries", []).append({"failure": name, **result})
        assert result["verified"] is True, (name, result)
        self.active.remove(name)

    def still_faulty(self, name: str) -> None:
        ctx = self.contexts[name]
        assert ctx.problem.verify_fault(ctx.parsed)["verified"], (
            f"{name} disappeared when the other failure was recovered"
        )

    def own_symptom(self, stage: str, name: str) -> bool:
        ctx = self.contexts[name]
        kwargs: dict[str, Any] = {
            "scenario": ctx.scenario,
            "topo_size": ctx.topo_size,
            "problem": ctx.problem,
        }
        if ctx.before is not None and hasattr(ctx.before, "ping_ok"):
            kwargs["before"] = ctx.before
        ok, details = evaluate_symptom(
            ctx.runtime, ctx.problem.root_cause_name, ctx.parsed, **kwargs
        )
        self.evidence[f"{stage}: {name} symptom"] = {"observed": ok, **details}
        return ok

    def _context(self) -> ProbeContext:
        first = next(iter(self.contexts.values()))
        return ProbeContext(
            runtime=first.runtime,
            net_env=first.problem.net_env,
            inject={
                name: dict(ctx.parsed.model_dump(mode="json"))
                for name, ctx in self.contexts.items()
            },
            problems={name: ctx.problem for name, ctx in self.contexts.items()},
        )

    def outage(self, stage: str) -> bool:
        ok, details = self.check.probe(self._context())
        self.evidence[f"{stage}: outage"] = details
        assert "error" not in details, details
        return ok

    def masked_symptom(self, stage: str, masked: str) -> bool:
        if self.check.probe is None:
            return self.own_symptom(stage, masked)
        ok, details = self.check.probe(self._context())
        self.evidence[f"{stage}: {masked} exposed"] = details
        return ok

    def expect_outage(self, stage: str, expected: bool) -> None:
        # Routing reconverges after each change, through failover when only
        # one failure is active; judge the settled state.
        time.sleep(SETTLE_SEC)
        assert wait_until(
            lambda: self.outage(stage) == expected,
            OUTAGE_TIMEOUT_SEC,
            interval_s=10,
        ), self.evidence

    def expect_masked(self, stage: str, masked: str, expected: bool) -> None:
        # Controllers apply a change asynchronously, so poll until it settles.
        assert wait_until(
            lambda: self.masked_symptom(stage, masked) == expected,
            OUTAGE_TIMEOUT_SEC,
            interval_s=10,
        ), self.evidence

    def run(self) -> None:
        try:
            if self.check.mode == "masked":
                self._masking()
            else:
                self._joint_outage()
            from nika.problems.support.recovery import verify_recovered_environment

            first = next(iter(self.contexts.values()))
            self.evidence["healthy_after_recovery"] = verify_recovered_environment(
                first.problem.net_env
            )
        except BaseException:
            # Restore what is still injected without hiding the failure.
            for name in reversed(list(self.active)):
                try:
                    self.recover(name)
                except Exception:  # noqa: BLE001
                    pass
            raise

    def _joint_outage(self) -> None:
        first, last = (
            self.combination.instance_keys[0],
            self.combination.instance_keys[-1],
        )
        self.expect_outage("healthy", False)
        for name in self.combination.instance_keys:
            self.inject(name)
            self.expect_outage(f"{name} alone", False)
            self.own_symptom(f"{name} alone", name)
            self.recover(name)
        for name in self.combination.instance_keys:
            self.inject(name)
        self.expect_outage("both", True)
        # Each failure is a root cause: repairing the other leaves it in place,
        # and repairing either restores service.
        self.recover(last)
        self.expect_outage(f"{last} recovered", False)
        self.still_faulty(first)
        self.inject(last)
        self.expect_outage("both again", True)
        self.recover(first)
        self.expect_outage(f"{first} recovered", False)
        self.still_faulty(last)
        self.recover(last)

    def _masking(self) -> None:
        masked, masker = (
            self.combination.instance_keys[0],
            self.combination.instance_keys[-1],
        )
        for name in (masked, masker):
            self.inject(name)
            assert self.own_symptom(f"{name} alone", name), self.evidence
            if name == masked:
                self.expect_masked(f"{name} alone", masked, True)
            self.recover(name)
        self.inject(masked)
        self.inject(masker)
        self.expect_masked("both", masked, False)
        self.recover(masker)
        self.still_faulty(masked)
        self.expect_masked(f"{masker} recovered", masked, True)
        self.recover(masked)
