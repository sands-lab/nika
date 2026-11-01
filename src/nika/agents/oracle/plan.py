"""Build a task's reference diagnosis plan from a session or a benchmark row."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nika.problems.support.oracle import (
    OraclePlaybook,
    OracleStep,
    healthy_playbook,
    triage,
)
from nika.workflows.benchmark.healthy import HEALTHY_PROBLEM

# Benchmark row fields that select the deployment a failure instance targets.
_DEPLOY_KEYS = (
    "topo_size",
    "topo",
    "igp",
    "bgp_mode",
    "rpki",
    "backend",
    "device_profile",
)


@dataclass
class FaultPlan:
    """Reference playbook and expected root causes for one injected fault."""

    fault_type: str
    playbook: OraclePlaybook
    root_causes: list[dict[str, str]]


def fault_plan(
    problem_name: str,
    scenario: str,
    scenario_params: dict[str, Any],
    inject: dict[str, Any],
    *,
    release: str | None = None,
) -> FaultPlan:
    """Instantiate *problem_name* on *scenario* and build its playbook."""
    from nika.problems.registry import get_problem_instance

    if problem_name == HEALTHY_PROBLEM:
        return FaultPlan(HEALTHY_PROBLEM, healthy_playbook(), [])
    problem = get_problem_instance(
        [problem_name], scenario, release=release, **scenario_params
    )
    params = problem.resolve_params(dict(inject))
    playbook = problem.oracle_playbook(params)
    if playbook.anomaly and not any(step.evidence for step in playbook.steps):
        raise ValueError(
            f"{problem_name} playbook has no evidence step for this target"
        )
    causes = [
        {"resource_id": c.resource_id, "fault_type": c.fault_type}
        for c in problem.get_ground_truth().root_causes
    ]
    return FaultPlan(problem_name, playbook, causes)


def row_plans(row: dict[str, Any], *, release: str | None = None) -> list[FaultPlan]:
    """Plans for every fault of a benchmark case row, built without a lab."""
    from nika.workflows.benchmark.multi_fault import nested_inject_map, row_problems

    scenario = str(row["scenario"])
    params = {k: row[k] for k in _DEPLOY_KEYS if row.get(k) not in (None, "")}
    if row.get("problem") == HEALTHY_PROBLEM:
        return [fault_plan(HEALTHY_PROBLEM, scenario, params, {})]
    injects = nested_inject_map(row)
    return [
        fault_plan(name, scenario, params, injects.get(name, {}), release=release)
        for name in row_problems(row)
    ]


def triaged_steps(playbooks: list[OraclePlaybook | None]) -> list[list[OracleStep]]:
    """Steps to run per playbook; the first fault playbook starts with triage."""
    step_lists: list[list[OracleStep]] = []
    triaged = False
    for playbook in playbooks:
        steps = list(playbook.steps) if playbook is not None else []
        if playbook is not None and playbook.anomaly and not triaged:
            steps.insert(0, triage())
            triaged = True
        step_lists.append(steps)
    return step_lists


def describe_plans(plans: list[FaultPlan]) -> list[dict[str, Any]]:
    """YAML-friendly view of *plans* for ``nika benchmark describe --oracle``."""
    views = []
    step_lists = triaged_steps([plan.playbook for plan in plans])
    for plan, plan_steps in zip(plans, step_lists, strict=True):
        steps = []
        for index, step in enumerate(plan_steps, start=1):
            args = "<from earlier output>" if callable(step.args) else dict(step.args)
            item: dict[str, Any] = {"step": index, "purpose": step.purpose}
            item["call"] = {"tool": step.tool, "args": args}
            if step.expect is not None:
                item["expect"] = step.expect.description
            if step.evidence:
                item["evidence"] = True
            steps.append(item)
        views.append(
            {
                "fault_type": plan.fault_type,
                "finding": plan.playbook.finding,
                "steps": steps,
                "submit": plan.root_causes,
            }
        )
    return views
