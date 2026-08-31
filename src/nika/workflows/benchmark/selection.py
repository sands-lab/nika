"""Coverage-guided greedy selection of benchmark cases."""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from nika.workflows.benchmark.candidate_context import (
    deployment_environment_key,
    enrich_candidate,
    normalize_topo_scale,
    row_to_case,
    selection_context_key,
)
from nika.workflows.benchmark.e2e_validation import isp_containerlab_node_count
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.resume import benchmark_row_fingerprint


GAIN_FAILURE_SCENARIO = 100
GAIN_SCENARIO_SCALE = 50
GAIN_DOMAIN_SCALE = 50
PENALTY_DUPLICATE_CONTEXT = 10_000
MAX_FAILURE_ENVIRONMENTS = 2


@dataclass
class CoverageState:
    failure_covered: set[str] = field(default_factory=set)
    failure_scenario: set[tuple[str, str]] = field(default_factory=set)
    scenario_scale: set[tuple[str, str]] = field(default_factory=set)
    failure_domain_scale: set[tuple[str, str]] = field(default_factory=set)
    healthy_environment: set[tuple[str, str, str, str, bool, str, str]] = field(
        default_factory=set
    )
    failure_pick_count: Counter[str] = field(default_factory=Counter)
    failure_context_picked: set[tuple[Any, ...]] = field(default_factory=set)
    failure_environments_by_problem: dict[
        str, set[tuple[str, str, str, str, bool, str, str]]
    ] = field(default_factory=dict)
    selected: list[dict[str, Any]] = field(default_factory=list)
    selected_option_ids: set[str] = field(default_factory=set)

    def add(self, candidate: dict[str, Any]) -> None:
        option_id = str(candidate.get("candidate_option_id") or "")
        if option_id:
            self.selected_option_ids.add(option_id)
        self.selected.append(candidate)
        problem = str(candidate["problem"])
        scenario = str(candidate["scenario"])
        scale = normalize_topo_scale(candidate)
        domain = str(candidate.get("failure_domain") or "")
        environment = deployment_environment_key(candidate)

        if is_healthy_case(problem):
            self.healthy_environment.add(environment)
            return

        self.failure_covered.add(problem)
        self.failure_scenario.add((problem, scenario))
        self.scenario_scale.add((scenario, scale))
        if domain:
            self.failure_domain_scale.add((domain, scale))
        self.failure_pick_count[problem] += 1
        self.failure_context_picked.add(selection_context_key(candidate))
        self.failure_environments_by_problem.setdefault(problem, set()).add(environment)

    def remove(self, candidate: dict[str, Any]) -> None:
        self.selected.remove(candidate)
        self._rebuild()

    def _rebuild(self) -> None:
        fresh = CoverageState()
        for item in self.selected:
            fresh.add(item)
        self.__dict__.update(fresh.__dict__)


def _scale_balance_score(scale: str, state: CoverageState) -> int:
    counts = Counter(normalize_topo_scale(row) for row in state.selected)
    if scale == "fixed":
        return 0
    order = {"s": 0, "m": 1, "l": 2}
    return counts.get(scale, 0) * 10 + order.get(scale, 0)


def _scenario_balance_score(scenario: str, state: CoverageState) -> int:
    counts = Counter(str(row["scenario"]) for row in state.selected)
    return counts.get(scenario, 0) * 5


def compute_gain(candidate: dict[str, Any], state: CoverageState) -> int:
    problem = str(candidate["problem"])
    scenario = str(candidate["scenario"])
    scale = normalize_topo_scale(candidate)
    domain = str(candidate.get("failure_domain") or "")

    if is_healthy_case(problem):
        return (
            1
            if deployment_environment_key(candidate) not in state.healthy_environment
            else 0
        )

    ctx = selection_context_key(candidate)
    if ctx in state.failure_context_picked:
        return -PENALTY_DUPLICATE_CONTEXT

    count = state.failure_pick_count[problem]
    if count >= MAX_FAILURE_ENVIRONMENTS:
        return -PENALTY_DUPLICATE_CONTEXT

    gain = 0
    if (problem, scenario) not in state.failure_scenario:
        gain += GAIN_FAILURE_SCENARIO
    if (scenario, scale) not in state.scenario_scale:
        gain += GAIN_SCENARIO_SCALE
    if domain and (domain, scale) not in state.failure_domain_scale:
        gain += GAIN_DOMAIN_SCALE
    return gain


def _containerlab_size_score(candidate: dict[str, Any]) -> int:
    """Prefer fewer routers when choosing Containerlab ISP variants."""
    if str(candidate.get("backend") or "") != "containerlab":
        return 0
    nodes = isp_containerlab_node_count(str(candidate.get("scenario") or ""))
    return 0 if nodes is None else nodes


def _tie_key(candidate: dict[str, Any], state: CoverageState) -> tuple[Any, ...]:
    scenario = str(candidate["scenario"])
    scale = normalize_topo_scale(candidate)
    return (
        -compute_gain(candidate, state),
        _containerlab_size_score(candidate),
        _scale_balance_score(scale, state),
        _scenario_balance_score(scenario, state),
        int(candidate.get("selection_tie_rank") or 0),
    )


def _sorted_candidates(
    candidates: list[dict[str, Any]],
    state: CoverageState,
) -> list[dict[str, Any]]:
    return sorted(candidates, key=lambda row: _tie_key(row, state))


def _is_selected(row: dict[str, Any], state: CoverageState) -> bool:
    option_id = str(row.get("candidate_option_id") or "")
    return bool(option_id and option_id in state.selected_option_ids)


def _pick_best(
    pool: list[dict[str, Any]],
    state: CoverageState,
    *,
    predicate,
) -> dict[str, Any] | None:
    eligible = [row for row in pool if predicate(row) and not _is_selected(row, state)]
    if not eligible:
        return None
    ranked = _sorted_candidates(eligible, state)
    return ranked[0]


def _phase_p0(
    failures: list[str],
    pool: list[dict[str, Any]],
    state: CoverageState,
) -> None:
    failure_pool = [row for row in pool if not row.get("is_healthy")]
    for problem in failures:
        if problem in state.failure_covered:
            continue
        problem_rows = [row for row in failure_pool if str(row["problem"]) == problem]
        pool_backends = {
            str(row.get("backend") or "")
            for row in problem_rows
            if str(row.get("backend") or "")
        }

        def _covers_failure(row: dict[str, Any], *, problem=problem) -> bool:
            return str(row["problem"]) == problem

        def _covers_with_stack(row: dict[str, Any], *, problem=problem) -> bool:
            return (
                str(row["problem"]) == problem and bool(str(row.get("backend") or ""))
            )

        pick = None
        # Dual-backend failures: seed the first pick on a stacked ISP/device variant
        # so the second env can fill the opposite backend within MAX=2.
        if len(pool_backends) >= 2:
            pick = _pick_best(failure_pool, state, predicate=_covers_with_stack)
        if pick is None:
            pick = _pick_best(failure_pool, state, predicate=_covers_failure)
        if pick is None:
            raise ValueError(f"No candidate available for failure {problem!r}")
        state.add(pick)


def _phase_cross_environment_repeats(
    failures: list[str],
    pool: list[dict[str, Any]],
    state: CoverageState,
) -> None:
    for problem in failures:
        problem_rows = [
            row
            for row in pool
            if not row.get("is_healthy") and str(row["problem"]) == problem
        ]
        environments = {deployment_environment_key(row) for row in problem_rows}
        required = min(MAX_FAILURE_ENVIRONMENTS, len(environments))
        pool_backends = {
            str(row.get("backend") or "")
            for row in problem_rows
            if str(row.get("backend") or "")
        }
        while state.failure_pick_count[problem] < required:
            picked = state.failure_environments_by_problem.get(problem, set())
            picked_backends = {
                str(row.get("backend") or "")
                for row in state.selected
                if (
                    not row.get("is_healthy")
                    and str(row["problem"]) == problem
                    and str(row.get("backend") or "")
                )
            }
            missing_backends = pool_backends - picked_backends

            def _new_environment(row: dict[str, Any], *, problem=problem) -> bool:
                return (
                    not row.get("is_healthy")
                    and str(row["problem"]) == problem
                    and deployment_environment_key(row) not in picked
                )

            def _fills_missing_backend(
                row: dict[str, Any], *, problem=problem
            ) -> bool:
                if not _new_environment(row, problem=problem):
                    return False
                backend = str(row.get("backend") or "")
                return bool(backend) and backend in missing_backends

            pick = None
            if missing_backends:
                pick = _pick_best(pool, state, predicate=_fills_missing_backend)
            if pick is None:
                pick = _pick_best(pool, state, predicate=_new_environment)
            if pick is None:
                raise ValueError(
                    f"Cannot select {required} environments for failure {problem!r}"
                )
            state.add(pick)


def _phase_healthy_controls(
    pool: list[dict[str, Any]],
    state: CoverageState,
) -> None:
    selected_environments = sorted(
        {
            deployment_environment_key(row)
            for row in state.selected
            if not row.get("is_healthy")
        }
    )
    healthy_by_environment = {
        deployment_environment_key(row): row for row in pool if row.get("is_healthy")
    }
    for environment in selected_environments:
        control = healthy_by_environment.get(environment)
        if control is None:
            raise ValueError(f"No healthy control for environment {environment!r}")
        state.add(control)


def select_benchmark_cases(
    candidates: list[dict[str, Any]],
    *,
    seed: int = 42,
) -> tuple[list[dict[str, Any]], CoverageState]:
    """Run coverage-guided selection over collapsed candidates."""
    pool = [enrich_candidate(row) for row in candidates]
    rng = random.Random(seed)
    seeded_order = sorted(
        pool, key=lambda row: str(row.get("candidate_option_id") or "")
    )
    rng.shuffle(seeded_order)
    for rank, row in enumerate(seeded_order):
        row["selection_tie_rank"] = rank

    healthy_environments = {
        deployment_environment_key(row) for row in pool if row.get("is_healthy")
    }
    controlled_pool = [
        row
        for row in pool
        if row.get("is_healthy")
        or deployment_environment_key(row) in healthy_environments
    ]
    failures_in_pool = sorted(
        {str(row["problem"]) for row in pool if not row.get("is_healthy")}
    )
    controlled_failures = {
        str(row["problem"]) for row in controlled_pool if not row.get("is_healthy")
    }
    missing_controls = sorted(set(failures_in_pool) - controlled_failures)
    if missing_controls:
        raise ValueError(
            f"Failures have no environment-matched healthy control: {missing_controls}"
        )
    state = CoverageState()

    _phase_p0(failures_in_pool, controlled_pool, state)
    _phase_cross_environment_repeats(failures_in_pool, controlled_pool, state)
    _phase_healthy_controls(controlled_pool, state)

    cases = [row_to_case(row) for row in state.selected]
    return cases, state


def baseline_one_per_failure(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pick one canonical case per failure for coverage comparison."""
    pool = [enrich_candidate(row) for row in candidates if not row.get("is_healthy")]
    by_failure: dict[str, dict[str, Any]] = {}
    for row in sorted(
        pool, key=lambda item: str(item.get("candidate_option_id") or "")
    ):
        problem = str(row["problem"])
        by_failure.setdefault(problem, row)
    return [row_to_case(row) for row in by_failure.values()]


def selection_fingerprints(cases: list[dict[str, Any]]) -> list[str]:
    return [benchmark_row_fingerprint(case) for case in cases]
