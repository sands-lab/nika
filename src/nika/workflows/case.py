"""Browse catalog presets and launch one through the normal session lifecycle."""

from pathlib import Path
from typing import Any

from nika.config import BENCHMARK_DIR
from nika.net_env.net_env_pool import (
    resolve_scenario_backend,
    scenario_requires_topo_size,
)
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.isp_options import ISP_DEPLOY_KEYS
from nika.workflows.benchmark.load_config import (
    load_benchmark_input,
    normalize_benchmark_row,
)
from nika.workflows.benchmark.candidate_context import pool_context_key
from nika.workflows.benchmark.trials import task_id_for_row

DEFAULT_CATALOG = BENCHMARK_DIR / "working" / "pool"
DEPLOY_KEYS = ("topo_size", *ISP_DEPLOY_KEYS)


def load_example_cases(
    catalog: Path | None = None, *, env: str | None = None, failure: str | None = None
) -> list[dict[str, Any]]:
    """Use catalog presets and fill missing registered contexts for default browsing."""
    cases: dict[str, dict[str, Any]] = {}
    for row in load_benchmark_input(catalog or DEFAULT_CATALOG):
        if (env and row["scenario"] != env) or (failure and row["problem"] != failure):
            continue
        if is_healthy_case(row["problem"]) or len(row.get("problems", [])) > 1:
            continue
        cls = get_problem_class(row["problem"], row["scenario"])
        if cls is None or not cls.is_compatible(row["scenario"]):
            continue
        backend = resolve_scenario_backend(
            row["scenario"],
            backend=row.get("backend"),
            default_when_ambiguous="kathara",
        )
        if cls.supported_backends and backend not in cls.supported_backends:
            continue
        cases[task_id_for_row(row)] = {
            **row,
            "case_source": "catalog",
            "experimental": True,
        }
    if catalog is None:
        from nika.workflows.benchmark.generate import (
            build_failure_cases,
            iter_failure_case_specs,
        )
        from nika.utils.logger import log_warning_event

        covered = {pool_context_key(row) for row in cases.values()}
        for problem, scenario, size, options in iter_failure_case_specs(
            include_benchmark_excluded=True
        ):
            if (env and scenario != env) or (failure and problem != failure):
                continue
            context = {
                "scenario": scenario,
                "problem": problem,
                "topo_size": size,
                **(options or {}),
            }
            if pool_context_key(context) in covered:
                continue
            generated, rejected = build_failure_cases(
                problem=problem, scenario=scenario, topo_size=size, isp_options=options
            )
            for item in generated:
                row = normalize_benchmark_row(
                    {"scenario": scenario, "problem": problem, **item}
                )
                cases.setdefault(
                    task_id_for_row(row),
                    {**row, "case_source": "generated", "experimental": True},
                )
            if not generated:
                log_warning_event(
                    "case_unavailable",
                    f"No legal injection preset for {scenario}/{problem}/{size or '-'}",
                    scenario=scenario,
                    problem=problem,
                    rejected=rejected,
                )
    return sorted(
        cases.values(),
        key=lambda row: (
            row["scenario"],
            {"": 0, "s": 0, "m": 1, "l": 2}.get(row.get("topo_size", ""), 3),
            row.get("backend", "kathara") != "kathara",
            task_id_for_row(row),
        ),
    )


def start_example_case(row: dict[str, Any], *, result_dir: str | None = None) -> str:
    """Validate, deploy an isolated lab, and inject the preset; leave it running."""
    from nika.workflows.benchmark.inject_resolve import validate_benchmark_case
    from nika.workflows.env.start import start_net_env
    from nika.workflows.failure.inject import inject_failure
    from nika.workflows.session.close import close_session

    options = {key: row[key] for key in ISP_DEPLOY_KEYS if key in row}
    validate_benchmark_case(
        row["scenario"],
        row["problem"],
        row["inject"],
        row.get("topo_size", ""),
        isp_options=options,
    )
    cls = get_problem_class(row["problem"], row["scenario"])
    params_class = getattr(cls, "Params", None)
    if params_class is not None:
        params_class.model_validate(row["inject"])
    size = (
        row.get("topo_size") if scenario_requires_topo_size(row["scenario"]) else None
    )
    session_id = start_net_env(
        row["scenario"], size, instance_tag="case", result_dir=result_dir, **options
    )
    try:
        inject_failure(
            [row["problem"]],
            session_id=session_id,
            param_overrides=row["inject"],
            expected_root_causes=row.get("root_causes"),
        )
    except BaseException as exc:
        try:
            close_session(
                session_id=session_id,
                status="error" if isinstance(exc, Exception) else "aborted",
            )
        except Exception as cleanup_exc:
            from nika.utils.logger import log_warning_event

            log_warning_event(
                "case_cleanup_failed",
                f"Could not close session {session_id} after failed injection: {cleanup_exc}",
                session_id=session_id,
            )
        raise
    return session_id
