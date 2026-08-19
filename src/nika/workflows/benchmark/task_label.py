"""Compound benchmark task labels: ``{scenario}_{problem}`` / ``{scenario}_{size}_{problem}``."""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from typing import Any

from nika.config import BENCHMARK_DIR
from nika.net_env.net_env_pool import (
    DC_CLOS_SCENARIO,
    CAMPUS_LAN_SCENARIO,
    list_all_net_envs,
    resolve_scenario_ref,
    scenario_requires_topo_size,
)
from nika.problems.prob_pool import list_avail_problem_names

_SIZE_TOKENS = ("s", "m", "l")
_LEGACY_CLOS_PREFIXES = {
    "dc_clos_bgp": "host",
    "dc_clos_service": "service",
}
_LEGACY_CAMPUS_LAN_PREFIXES = {
    "ospf_enterprise_static": "static",
    "ospf_enterprise_dhcp": "dhcp",
}


def format_task_label(scenario: str, problem: str, topo_size: str | None = None) -> str:
    """Build a user-facing task label from scenario / size / problem."""
    canonical, _ = resolve_scenario_ref(scenario)
    size = (topo_size or "").strip()
    requires = scenario_requires_topo_size(canonical)
    if requires:
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


def parse_task_label(label: str) -> tuple[str, str, str, str | None]:
    """Parse ``LABEL`` into ``(scenario, topo_size, problem, workload)``.

    ``topo_size`` is ``\"s\"|\"m\"|\"l\"`` for scalable scenarios, else ``\"\"``.
    ``workload`` is set for Clos / campus_lan cases (including legacy prefixes).
    Raises ``ValueError`` when the label is unknown or ambiguous.
    """
    text = (label or "").strip()
    if not text:
        raise ValueError("Task label must be a non-empty string.")

    from nika.problems.prob_pool import list_avail_problem_instances

    problems = list_avail_problem_instances()
    matches: list[tuple[str, str, str, str | None]] = []
    for scenario, problem, size, _ignored in _iter_label_triples():
        if format_task_label(scenario, problem, size or None) == text:
            workload = None
            if scenario == DC_CLOS_SCENARIO and problem in problems:
                tags = set(problems[problem].TAGS)
                if problem in ("bgp_hijacking",) or tags & {"dns", "http"}:
                    workload = "service"
                else:
                    workload = "host"
            elif scenario == CAMPUS_LAN_SCENARIO and problem in problems:
                tags = set(problems[problem].TAGS)
                if problem in (
                    "host_incorrect_ip",
                    "host_incorrect_netmask",
                    "host_missing_ip",
                ):
                    workload = "static"
                elif tags & {"dhcp", "dns", "load_balancer", "web"}:
                    workload = "dhcp"
                else:
                    workload = "dhcp"
            matches.append((scenario, size, problem, workload))
        for legacy, legacy_workload in _LEGACY_CLOS_PREFIXES.items():
            legacy_label = (
                f"{legacy}_{size}_{problem}" if size else f"{legacy}_{problem}"
            )
            if text == legacy_label:
                matches.append((DC_CLOS_SCENARIO, size, problem, legacy_workload))
        for legacy, legacy_workload in _LEGACY_CAMPUS_LAN_PREFIXES.items():
            legacy_label = (
                f"{legacy}_{size}_{problem}" if size else f"{legacy}_{problem}"
            )
            if text == legacy_label:
                matches.append((CAMPUS_LAN_SCENARIO, size, problem, legacy_workload))

    # Deduplicate identical parses
    unique: list[tuple[str, str, str, str | None]] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for item in matches:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    if len(unique) == 1:
        return unique[0]
    if not unique:
        raise ValueError(
            f"Unknown task label {label!r}. "
            "Expected {scenario}_{problem} or {scenario}_{s|m|l}_{problem} "
            "(see `nika env list` and `nika failure list`)."
        )
    rendered = ", ".join(
        f"{scenario}/{problem}"
        + (f" size={size}" if size else "")
        + (f" workload={workload}" if workload else "")
        for scenario, size, problem, workload in unique
    )
    raise ValueError(f"Ambiguous task label {label!r}; matches: {rendered}.")


def resolve_default_inject_params(
    scenario: str,
    problem: str,
    topo_size: str = "",
    *,
    overrides: dict[str, str] | None = None,
    workload: str | None = None,
) -> dict[str, str]:
    """Resolve default inject params for a label, then apply optional overrides."""
    resolve = _load_resolve_inject_params()
    params = dict(resolve(problem, scenario, topo_size or "", workload=workload))
    if overrides:
        params.update(overrides)
    return params


def _iter_label_triples() -> list[tuple[str, str, str, str | None]]:
    scenarios = list(list_all_net_envs())
    problems = list_avail_problem_names()
    triples: list[tuple[str, str, str, str | None]] = []
    for scenario in scenarios:
        if scenario_requires_topo_size(scenario):
            for size in _SIZE_TOKENS:
                for problem in problems:
                    triples.append((scenario, problem, size, None))
        else:
            for problem in problems:
                triples.append((scenario, problem, "", None))
    return triples


@lru_cache(maxsize=1)
def _load_resolve_inject_params() -> Any:
    """Load ``resolve_inject_params`` from ``benchmark/inject_resolve.py``."""
    path = BENCHMARK_DIR / "inject_resolve.py"
    if not path.is_file():
        raise FileNotFoundError(f"Missing inject resolver at {path}")
    spec = importlib.util.spec_from_file_location("nika_benchmark_inject_resolve", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load inject resolver from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, "resolve_inject_params", None)
    if fn is None:
        raise ImportError(f"{path} has no resolve_inject_params")
    return fn
