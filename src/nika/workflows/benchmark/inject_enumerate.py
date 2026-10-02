"""Semantic inject-target enumeration built on the canonical resolver."""

from __future__ import annotations

from nika.problems.support.benchmark_targets import unique as _unique
from nika.workflows.benchmark.inject_resolve import (
    DEFAULT_SEED,
    _benchmark_problem_class,
    _get_net_env_for_benchmark,
    _load_inventory,
    _resolve,
)


def enumerate_inject_params(
    problem: str,
    scenario: str,
    topo_size: str = "",
    *,
    isp_options: dict[str, str] | None = None,
    net_env=None,
) -> list[dict[str, str]]:
    """Return legal target variants while retaining canonical auxiliary knobs."""
    if net_env is None:
        net_env = _get_net_env_for_benchmark(
            scenario, topo_size, isp_options=isp_options
        )
    _load_inventory(net_env)
    base, ctx = _resolve(
        problem,
        scenario,
        topo_size,
        seed=DEFAULT_SEED,
        isp_options=isp_options,
        net_env=net_env,
    )
    problem_cls = _benchmark_problem_class(problem)
    return _unique(problem_cls.benchmark_inject_options(ctx, base))
