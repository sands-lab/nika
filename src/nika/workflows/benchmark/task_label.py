"""Compound benchmark task labels: ``{scenario}_{problem}`` / ``{scenario}_{size}_{problem}``."""

from __future__ import annotations

import importlib.util
from functools import lru_cache
from typing import Any

from nika.config import BENCHMARK_DIR
from nika.net_env.net_env_pool import list_all_net_envs, scenario_requires_topo_size
from nika.problems.prob_pool import list_avail_problem_names

_SIZE_TOKENS = ("s", "m", "l")


def format_task_label(scenario: str, problem: str, topo_size: str | None = None) -> str:
    """Build a user-facing task label from scenario / size / problem."""
    size = (topo_size or "").strip()
    requires = scenario_requires_topo_size(scenario)
    if requires:
        if size not in _SIZE_TOKENS:
            raise ValueError(
                f"Scenario {scenario!r} requires topo_size in {_SIZE_TOKENS}; got {topo_size!r}."
            )
        return f"{scenario}_{size}_{problem}"
    if size:
        raise ValueError(
            f"Scenario {scenario!r} does not use sizes; omit topo_size (got {topo_size!r})."
        )
    return f"{scenario}_{problem}"


def parse_task_label(label: str) -> tuple[str, str, str]:
    """Parse ``LABEL`` into ``(scenario, topo_size, problem)``.

    ``topo_size`` is ``\"s\"|\"m\"|\"l\"`` for scalable scenarios, else ``\"\"``.
    Raises ``ValueError`` when the label is unknown or ambiguous.
    """
    text = (label or "").strip()
    if not text:
        raise ValueError("Task label must be a non-empty string.")

    matches: list[tuple[str, str, str]] = []
    for scenario, problem, size in _iter_canonical_triples():
        if format_task_label(scenario, problem, size or None) == text:
            matches.append((scenario, size, problem))

    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"Unknown task label {label!r}. "
            "Expected {scenario}_{problem} or {scenario}_{s|m|l}_{problem} "
            "(see `nika env list` and `nika failure list`)."
        )
    rendered = ", ".join(
        f"{scenario}/{problem}" + (f" size={size}" if size else "")
        for scenario, size, problem in matches
    )
    raise ValueError(f"Ambiguous task label {label!r}; matches: {rendered}.")


def resolve_default_inject_params(
    scenario: str,
    problem: str,
    topo_size: str = "",
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve default inject params for a label, then apply optional overrides."""
    resolve = _load_resolve_inject_params()
    params = dict(resolve(problem, scenario, topo_size or ""))
    if overrides:
        params.update(overrides)
    return params


def _iter_canonical_triples() -> list[tuple[str, str, str]]:
    scenarios = list(list_all_net_envs())
    problems = list_avail_problem_names()
    triples: list[tuple[str, str, str]] = []
    for scenario in scenarios:
        if scenario_requires_topo_size(scenario):
            for size in _SIZE_TOKENS:
                for problem in problems:
                    triples.append((scenario, problem, size))
        else:
            for problem in problems:
                triples.append((scenario, problem, ""))
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
