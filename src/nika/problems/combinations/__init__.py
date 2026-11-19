"""Multi-fault tasks: the tested catalog and the checks for user combinations.

``cases.yaml`` lists the multi-fault tasks a benchmark release may contain.
Each task fixes its deployment and every failure's parameters and passed the
live combination test. Benchmark files outside a release may combine any
registered failures; ``check_multi_fault_task`` rejects combinations that
cannot be injected, without promising that their symptoms appear.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from nika.problems.multi import fault_instance_keys

CATALOG = Path(__file__).with_name("cases.yaml")
# Deployment fields a catalog task may fix besides the scenario.
DEPLOY_KEYS = ("topo_size", "igp", "bgp_mode", "rpki", "backend", "device_profile")


@dataclass(frozen=True)
class FailureCombination:
    """Failures that are admitted together, with the tasks that realize them."""

    name: str
    description: str
    failures: tuple[str, ...]
    tasks: tuple[dict[str, Any], ...]
    # Public evidence that the failures occur together.
    sources: tuple[dict[str, Any], ...] = ()
    # Delay of every failure after the first, when the order matters.
    trigger_at: str | None = None
    # Time the network needs to converge after the last failure.
    settle: str = "0s"

    @property
    def instance_keys(self) -> list[str]:
        return fault_instance_keys(self.failures)

    def schedule(self, inject: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """Return the task schedule for ``inject``, or ``None`` when untimed."""
        if self.trigger_at is None:
            return None
        from nika.schedule import seconds

        start = seconds(self.trigger_at) + seconds(self.settle)
        return {
            "agent_start": f"{start:g}s",
            "events": [
                {
                    "problem": name,
                    "inject": dict(inject[key]),
                    "at": self.trigger_at if index else "0s",
                }
                for index, (name, key) in enumerate(
                    zip(self.failures, self.instance_keys, strict=True)
                )
            ],
        }

    def rows(self) -> list[dict[str, Any]]:
        """Benchmark rows of every task, without ground truth."""
        rows = []
        for task in self.tasks:
            row = {"scenario": task["scenario"]}
            row.update({key: task[key] for key in DEPLOY_KEYS if key in task})
            schedule = self.schedule(task["inject"])
            if schedule is None:
                row["problems"] = list(self.failures)
                row["inject"] = {
                    name: dict(task["inject"][name]) for name in self.instance_keys
                }
            else:
                row["schedule"] = schedule
            rows.append(row)
        return rows


@cache
def _catalog() -> dict[str, FailureCombination]:
    combinations = {}
    for entry in yaml.safe_load(CATALOG.read_text()):
        combination = FailureCombination(
            name=entry["name"],
            description=entry["description"],
            failures=tuple(entry["failures"]),
            tasks=tuple(entry["tasks"]),
            sources=tuple(entry.get("sources") or ()),
            trigger_at=entry.get("trigger_at"),
            settle=entry.get("settle", "0s"),
        )
        if not combination.sources:
            raise ValueError(f"{combination.name} needs at least one source")
        if len(combination.failures) < 2:
            raise ValueError(f"{combination.name} needs two failure placements")
        for task in combination.tasks:
            if set(task["inject"]) != set(combination.instance_keys):
                raise ValueError(f"{combination.name} task does not set every failure")
        if combination.name in combinations:
            raise ValueError(f"Duplicate failure combination {combination.name!r}")
        combinations[combination.name] = combination
    return combinations


def list_combinations() -> list[FailureCombination]:
    return [_catalog()[name] for name in sorted(_catalog())]


def get_combination(name: str) -> FailureCombination:
    try:
        return _catalog()[name]
    except KeyError:
        raise ValueError(f"Unknown failure combination {name!r}") from None


def _str_inject(params: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in params.items()}


def task_placement(row: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Per-failure params of a multi-fault row, scheduled or not."""
    if row.get("schedule"):
        return {
            str(event["problem"]): _str_inject(event.get("inject") or {})
            for event in row["schedule"]["events"]
            if event.get("problem")
        }
    return {
        str(name): _str_inject(params)
        for name, params in (row.get("inject") or {}).items()
    }


def _deployment(row: dict[str, Any]) -> dict[str, Any]:
    deploy = {key: row[key] for key in DEPLOY_KEYS if row.get(key) not in (None, "")}
    deploy.setdefault("topo_size", "")
    return deploy


def combination_for_row(row: dict[str, Any]) -> str | None:
    """The catalog combination whose task a multi-fault row reproduces.

    The deployment fields the task fixes, the failures in order, their
    params, and the timing must all match.
    """
    from nika.schedule import normalize_schedule

    timed = bool(row.get("schedule"))
    placement = task_placement(row)
    failures = tuple(placement) if timed else tuple(row.get("problems") or ())
    deploy = _deployment(row)
    for combination in list_combinations():
        if (
            combination.failures != failures
            or (combination.trigger_at is not None) != timed
        ):
            continue
        for task in combination.tasks:
            fixed = {key: task[key] for key in DEPLOY_KEYS if key in task}
            if (
                task["scenario"] == row.get("scenario")
                and all(deploy.get(key) == value for key, value in fixed.items())
                and {
                    name: _str_inject(task["inject"][name])
                    for name in combination.instance_keys
                }
                == placement
                and (
                    not timed
                    or normalize_schedule(row["schedule"]).case_value()
                    == normalize_schedule(
                        combination.schedule(task["inject"])
                    ).case_value()
                )
            ):
                return combination.name
    return None


def check_multi_fault_task(row: dict[str, Any]) -> None:
    """Reject a multi-fault task whose failures cannot all be injected.

    Each failure must be registered, compatible with the deployment, and have
    params that fit its schema and the topology. Passing does not mean the
    failures produce a joint symptom; only catalog tasks are tested for that.
    """
    from nika.workflows.benchmark.inject_resolve import validate_benchmark_case

    from nika.workflows.benchmark.multi_fault import row_problems

    if len(row_problems(row)) < 2:
        return
    placement = task_placement(row)
    if set(placement) != set(fault_instance_keys(row_problems(row))):
        raise ValueError(
            "Multi-fault task must provide parameters for every declared failure"
        )
    deploy = _deployment(row)
    topo_size = str(deploy.pop("topo_size") or "")
    errors = []
    from nika.problems.registry import get_problem_class

    for name, key in zip(
        row_problems(row), fault_instance_keys(row_problems(row)), strict=True
    ):
        params = placement[key]
        try:
            cls = get_problem_class(name)
            if cls is None:
                raise ValueError("unknown failure")
            if cls.Params is not None:
                cls.Params.model_validate(params)
            validate_benchmark_case(
                str(row["scenario"]),
                name,
                params,
                topo_size,
                isp_options=deploy or None,
                combined=True,
            )
        except ValueError as exc:  # pydantic errors are ValueErrors
            errors.append(f"{name}: {exc}")
    if errors:
        raise ValueError("Multi-fault task cannot be injected: " + "; ".join(errors))


__all__ = [
    "FailureCombination",
    "check_multi_fault_task",
    "combination_for_row",
    "get_combination",
    "list_combinations",
    "task_placement",
]
