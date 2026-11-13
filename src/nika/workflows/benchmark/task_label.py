"""Compound benchmark task labels."""

from __future__ import annotations

from nika.net_env.net_env_pool import (
    list_all_net_envs,
    resolve_scenario_id,
    scenario_requires_topo_size,
)
from nika.problems.registry import get_problem_class, resolve_problem_name
from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.workflows.benchmark.healthy import is_healthy_case

_SIZE_TOKENS = ("s", "m", "l")


def format_task_label(scenario: str, problem: str, topo_size: str | None = None) -> str:
    """Build a task label from a canonical scenario, size, and problem."""
    canonical = resolve_scenario_id(scenario)
    size = (topo_size or "").strip()
    if scenario_requires_topo_size(canonical):
        if size not in _SIZE_TOKENS:
            raise ValueError(
                f"Scenario {scenario!r} requires topo_size in {_SIZE_TOKENS}; got {topo_size!r}."
            )
        return f"{canonical}_{size}_{problem}"
    if size:
        raise ValueError(
            f"Scenario {scenario!r} does not use sizes; omit topo_size (got {topo_size!r})."
        )
    return f"{canonical}_{problem}"


def parse_task_label(label: str) -> tuple[str, str, str]:
    """Parse a canonical task label into ``(scenario, topo_size, problem)``."""
    text = (label or "").strip()
    if not text:
        raise ValueError("Task label must be a non-empty string.")
    from nika.net_env.compat.v010 import scenario_specs

    matches = []
    for scenario in {*list_all_net_envs(), *scenario_specs()}:
        sizes = _SIZE_TOKENS if scenario_requires_topo_size(scenario) else ("",)
        for size in sizes:
            prefix = format_task_label(scenario, "", size or None)
            if not text.startswith(prefix):
                continue
            problem = resolve_problem_name(text[len(prefix) :], scenario)
            if (
                is_healthy_case(problem)
                or get_problem_class(problem, scenario) is not None
            ):
                matches.append((scenario, size, problem))
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"Unknown task label {label!r}. Expected {{scenario}}_{{problem}} or {{scenario}}_{{s|m|l}}_{{problem}}."
        )
    raise ValueError(f"Ambiguous task label {label!r}.")


def resolve_default_inject_params(
    scenario: str,
    problem: str,
    topo_size: str = "",
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve default inject params for a task label."""
    params = dict(resolve_inject_params(problem, scenario, topo_size or ""))
    if overrides:
        params.update(overrides)
    return params
