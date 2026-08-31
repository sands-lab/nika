"""Build deterministic Dev/Test splits with held-out deployment contexts."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from nika.config import BENCHMARK_DIR
from nika.net_env.net_env_pool import resolve_scenario_backend
from nika.workflows.benchmark.candidate_context import (
    collapse_candidates,
    deployment_environment_key,
    normalize_topo_scale,
    row_to_case,
    selection_context_key,
)
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.load_config import load_candidate_catalog
from nika.workflows.benchmark.pool_audit import eligible_candidates
from nika.workflows.benchmark.resume import (
    benchmark_option_id,
    benchmark_row_fingerprint,
)
from nika.workflows.benchmark.selection import select_benchmark_cases

DEFAULT_POOL = BENCHMARK_DIR / "working" / "pool"
DEFAULT_OUTPUT = BENCHMARK_DIR / "working" / "release-candidate"
REQUIRED_FAMILIES = frozenset(
    {
        "campus",
        "dc_clos",
        "enterprise_branch",
        "isp",
        "sdn",
        "p4",
        "kubernetes",
        "llm_serving",
        "srl_clos",
    }
)
REQUIRED_SCALES = frozenset({"s", "m", "l", "fixed"})
REQUIRED_BACKENDS = frozenset({"kathara", "containerlab"})


def scenario_family(scenario: str) -> str:
    if scenario == "campus_lan":
        return "campus"
    if scenario == "dc_clos":
        return "dc_clos"
    if scenario == "enterprise_branch":
        return "enterprise_branch"
    if scenario.startswith("isp_"):
        return "isp"
    if scenario == "sdn_l3_clos":
        return "sdn"
    if scenario.startswith("p4_"):
        return "p4"
    if scenario == "k8s_lab":
        return "kubernetes"
    if scenario == "llmd_lab":
        return "llm_serving"
    if scenario == "min3clos":
        return "srl_clos"
    return scenario


def effective_backend(row: dict[str, Any]) -> str:
    explicit = str(row.get("backend") or "")
    if explicit:
        return explicit
    return resolve_scenario_backend(str(row["scenario"]))


def _seeded_rank(candidates: list[dict[str, Any]], seed: int) -> dict[str, int]:
    ordered = sorted(candidates, key=lambda row: str(row["candidate_option_id"]))
    random.Random(seed).shuffle(ordered)
    return {str(row["candidate_option_id"]): rank for rank, row in enumerate(ordered)}


def _strata(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        scenario_family(str(row["scenario"])),
        normalize_topo_scale(row),
        effective_backend(row),
    )


def _covered(rows: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    families: set[str] = set()
    scales: set[str] = set()
    backends: set[str] = set()
    for row in rows:
        family, scale, backend = _strata(row)
        families.add(family)
        scales.add(scale)
        backends.add(backend)
    return families, scales, backends


def _add_strata_cases(
    rows: list[dict[str, Any]],
    *,
    pool: list[dict[str, Any]],
    forbidden_contexts: set[tuple[Any, ...]],
    rank: dict[str, int],
) -> None:
    while True:
        families, scales, backends = _covered(rows)
        missing = (
            REQUIRED_FAMILIES - families,
            REQUIRED_SCALES - scales,
            REQUIRED_BACKENDS - backends,
        )
        if not any(missing):
            return
        selected_contexts = {selection_context_key(row) for row in rows}
        eligible = [
            row
            for row in pool
            if not is_healthy_case(row["problem"])
            and selection_context_key(row) not in selected_contexts
            and selection_context_key(row) not in forbidden_contexts
        ]
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for row in eligible:
            family, scale, backend = _strata(row)
            gain = sum(
                (
                    family in missing[0],
                    scale in missing[1],
                    backend in missing[2],
                )
            )
            if gain:
                ranked.append((-gain, rank[str(row["candidate_option_id"])], row))
        if not ranked:
            raise ValueError(f"Cannot cover required split strata: {missing}")
        ranked.sort(key=lambda item: (item[0], item[1]))
        rows.append(ranked[0][2])


def _healthy_count(failure_count: int) -> int:
    """Smallest count that makes healthy at least 10% of the split."""
    return max(1, math.ceil(failure_count / 9))


def _add_healthy_cases(
    rows: list[dict[str, Any]],
    *,
    healthy_pool: list[dict[str, Any]],
    forbidden_environments: set[tuple[Any, ...]],
    target: int,
    rank: dict[str, int],
    reference: list[dict[str, Any]] | None = None,
) -> None:
    selected_environments = {
        deployment_environment_key(row)
        for row in rows
        if is_healthy_case(row["problem"])
    }
    while len(selected_environments) < target:
        family_counts = Counter(scenario_family(str(row["scenario"])) for row in rows)
        scale_counts = Counter(normalize_topo_scale(row) for row in rows)
        backend_counts = Counter(effective_backend(row) for row in rows)
        ref_family = Counter(
            scenario_family(str(row["scenario"])) for row in (reference or [])
        )
        ref_scale = Counter(normalize_topo_scale(row) for row in (reference or []))
        ref_backend = Counter(effective_backend(row) for row in (reference or []))
        choices = []
        for row in healthy_pool:
            environment = deployment_environment_key(row)
            if (
                environment in selected_environments
                or environment in forbidden_environments
            ):
                continue
            family, scale, backend = _strata(row)
            novelty = sum(
                (
                    family_counts[family] == 0,
                    scale_counts[scale] == 0,
                    backend_counts[backend] == 0,
                )
            )
            divergence = (
                abs((family_counts[family] + 1) - ref_family[family])
                + abs((scale_counts[scale] + 1) - ref_scale[scale])
                + abs((backend_counts[backend] + 1) - ref_backend[backend])
                if reference is not None
                else family_counts[family]
                + scale_counts[scale]
                + backend_counts[backend]
            )
            choices.append(
                (
                    -novelty,
                    divergence,
                    rank[str(row["candidate_option_id"])],
                    row,
                )
            )
        if not choices:
            raise ValueError("No disjoint healthy deployment environment remains")
        choices.sort(key=lambda item: (item[0], item[1], item[2]))
        chosen = choices[0][3]
        rows.append(chosen)
        selected_environments.add(deployment_environment_key(chosen))


def validate_dev_test_split(
    dev_cases: list[dict[str, Any]],
    test_cases: list[dict[str, Any]],
    *,
    expected_failures: set[str] | None = None,
) -> None:
    if not dev_cases or not test_cases:
        raise ValueError("Both Dev and Test splits must be non-empty")
    dev_failures = {
        str(row["problem"]) for row in dev_cases if not is_healthy_case(row["problem"])
    }
    test_failures = {
        str(row["problem"]) for row in test_cases if not is_healthy_case(row["problem"])
    }
    if dev_failures != test_failures:
        raise ValueError("Dev/Test failure taxonomies differ")
    if expected_failures is not None and dev_failures != expected_failures:
        missing = sorted(expected_failures - dev_failures)
        extra = sorted(dev_failures - expected_failures)
        raise ValueError(
            f"Split failure coverage mismatch: missing={missing}, extra={extra}"
        )
    exact_overlap = {benchmark_row_fingerprint(row) for row in dev_cases} & {
        benchmark_row_fingerprint(row) for row in test_cases
    }
    if exact_overlap:
        raise ValueError(
            f"Dev/Test exact isolation failed: {len(exact_overlap)} shared case(s)"
        )
    context_overlap = {selection_context_key(row) for row in dev_cases} & {
        selection_context_key(row) for row in test_cases
    }
    if context_overlap:
        raise ValueError(
            "Dev/Test semantic isolation failed: "
            f"{len(context_overlap)} shared failure-context pair(s)"
        )
    for name, rows in (("Dev", dev_cases), ("Test", test_cases)):
        families, scales, backends = _covered(rows)
        if not REQUIRED_FAMILIES <= families:
            raise ValueError(
                f"{name} is missing scenario families: "
                f"{sorted(REQUIRED_FAMILIES - families)}"
            )
        if not REQUIRED_SCALES <= scales:
            raise ValueError(
                f"{name} is missing topology scales: {sorted(REQUIRED_SCALES - scales)}"
            )
        if not REQUIRED_BACKENDS <= backends:
            raise ValueError(
                f"{name} is missing backends: {sorted(REQUIRED_BACKENDS - backends)}"
            )
    test_healthy = sum(is_healthy_case(row["problem"]) for row in test_cases)
    test_ratio = test_healthy / len(test_cases)
    if not 0.10 <= test_ratio <= 0.20:
        raise ValueError(f"Test healthy ratio {test_ratio:.4f} is outside [0.10, 0.20]")
    if not any(is_healthy_case(row["problem"]) for row in dev_cases):
        raise ValueError("Dev must contain healthy cases")


def select_dev_test_cases(
    candidates: list[dict[str, Any]], *, seed: int = 42
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pool = collapse_candidates(candidates)
    for row in pool:
        row["candidate_option_id"] = str(
            row.get("candidate_option_id") or benchmark_option_id(row)
        )
    failure_pool = [row for row in pool if not is_healthy_case(row["problem"])]
    healthy_pool = [row for row in pool if is_healthy_case(row["problem"])]
    failures = sorted({str(row["problem"]) for row in failure_pool})
    rank = _seeded_rank(pool, seed)

    _cases, canonical_state = select_benchmark_cases(pool, seed=seed)
    canonical_by_failure: dict[str, dict[str, Any]] = {}
    for row in canonical_state.selected:
        if not is_healthy_case(row["problem"]):
            canonical_by_failure.setdefault(str(row["problem"]), row)

    dev = [canonical_by_failure[problem] for problem in failures]
    test: list[dict[str, Any]] = []
    dev_family = Counter(scenario_family(str(row["scenario"])) for row in dev)
    dev_scale = Counter(normalize_topo_scale(row) for row in dev)
    dev_backend = Counter(effective_backend(row) for row in dev)
    test_family: Counter[str] = Counter()
    test_scale: Counter[str] = Counter()
    test_backend: Counter[str] = Counter()
    by_problem: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in failure_pool:
        by_problem[str(row["problem"])].append(row)
    for problem in failures:
        dev_row = canonical_by_failure[problem]
        dev_env = deployment_environment_key(dev_row)
        options = [
            row
            for row in by_problem[problem]
            if deployment_environment_key(row) != dev_env
        ]
        if not options:
            raise ValueError(f"Failure {problem!r} has no held-out deployment context")
        dev_stack = (
            effective_backend(dev_row),
            str(dev_row.get("device_profile") or ""),
        )
        ranked = []
        for row in options:
            new_scenario = str(row["scenario"]) != str(dev_row["scenario"])
            stack = (effective_backend(row), str(row.get("device_profile") or ""))
            new_stack = stack != dev_stack
            new_scale = normalize_topo_scale(row) != normalize_topo_scale(dev_row)
            family, scale, backend = _strata(row)
            balance = (
                test_family[family] / max(dev_family[family], 1)
                + test_scale[scale] / max(dev_scale[scale], 1)
                + test_backend[backend] / max(dev_backend[backend], 1)
            )
            ranked.append(
                (
                    not new_scenario,
                    not new_stack,
                    not new_scale,
                    balance,
                    rank[str(row["candidate_option_id"])],
                    row,
                )
            )
        ranked.sort(key=lambda item: item[:5])
        chosen = ranked[0][5]
        test.append(chosen)
        family, scale, backend = _strata(chosen)
        test_family[family] += 1
        test_scale[scale] += 1
        test_backend[backend] += 1

    _add_strata_cases(
        dev,
        pool=failure_pool,
        forbidden_contexts={selection_context_key(row) for row in test},
        rank=rank,
    )
    _add_strata_cases(
        test,
        pool=failure_pool,
        forbidden_contexts={selection_context_key(row) for row in dev},
        rank=rank,
    )

    _add_healthy_cases(
        dev,
        healthy_pool=healthy_pool,
        forbidden_environments=set(),
        target=_healthy_count(len(dev)),
        rank=rank,
    )
    _add_healthy_cases(
        test,
        healthy_pool=healthy_pool,
        forbidden_environments={
            deployment_environment_key(row)
            for row in dev
            if is_healthy_case(row["problem"])
        },
        target=_healthy_count(len(test)),
        rank=rank,
        reference=dev,
    )

    dev_cases = [row_to_case(row) for row in dev]
    test_cases = [row_to_case(row) for row in test]
    validate_dev_test_split(dev_cases, test_cases, expected_failures=set(failures))
    return dev_cases, test_cases


def write_split_catalog(
    *,
    pool: Path | None = None,
    output: Path | None = None,
    seed: int = 42,
    skip_audit: bool = False,
) -> dict[str, int]:
    pool_path = pool or DEFAULT_POOL
    output_path = output or DEFAULT_OUTPUT
    candidates = (
        collapse_candidates(load_candidate_catalog(pool_path))
        if skip_audit
        else eligible_candidates(str(pool_path))
    )
    dev, test = select_dev_test_cases(candidates, seed=seed)
    output_path.mkdir(parents=True, exist_ok=True)
    for name, cases in (("dev", dev), ("test", test)):
        (output_path / f"{name}.yaml").write_text(
            yaml.safe_dump({"seed": seed, "cases": cases}, sort_keys=False),
            encoding="utf-8",
        )
    return {"dev": len(dev), "test": len(test)}
