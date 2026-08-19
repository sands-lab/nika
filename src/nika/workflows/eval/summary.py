"""Aggregate finished session artifacts under results/ into a summary CSV."""

from __future__ import annotations

import json
from pathlib import Path

from nika.evaluator.result_log import (
    build_eval_result_from_session_dir,
    default_summary_csv_path,
    missing_summary_artifacts,
    resolve_root_cause_category,
    write_eval_summary_csv,
)
from nika.utils.session_artifacts import (
    RUN_FILENAME,
    is_finished_session,
    iter_session_dirs,
)


def _matches_filters(
    session_dir: Path,
    run_meta: dict,
    *,
    problems: set[str] | None,
    envs: set[str] | None,
    categories: set[str] | None,
    session_ids: set[str] | None,
    agent_types: set[str] | None,
    models: set[str] | None,
) -> bool:
    session_label = run_meta.get("session_id") or session_dir.name
    if session_ids and session_label not in session_ids:
        return False
    if problems:
        names = run_meta.get("problem_names") or []
        if isinstance(names, str):
            names = [names]
        if not any(name in problems for name in names):
            return False
    if envs and run_meta.get("scenario_name") not in envs:
        return False
    if categories:
        category = resolve_root_cause_category(run_meta)
        if category not in categories:
            return False
    if agent_types and run_meta.get("agent_type") not in agent_types:
        return False
    if models and run_meta.get("model") not in models:
        return False
    return True


def run_eval_summary(
    *,
    output_path: str | None = None,
    problems: list[str] | None = None,
    envs: list[str] | None = None,
    categories: list[str] | None = None,
    session_ids: list[str] | None = None,
    agent_types: list[str] | None = None,
    models: list[str] | None = None,
    results_dir: str | None = None,
) -> Path:
    """Scan finished sessions under results/, apply filters, and write one CSV file."""
    problem_set = set(problems) if problems else None
    env_set = set(envs) if envs else None
    category_set = set(categories) if categories else None
    session_id_set = set(session_ids) if session_ids else None
    agent_type_set = set(agent_types) if agent_types else None
    model_set = set(models) if models else None

    selected: list[Path] = []

    for session_dir in iter_session_dirs(results_dir):
        run_meta = json.loads((session_dir / RUN_FILENAME).read_text(encoding="utf-8"))

        if not _matches_filters(
            session_dir,
            run_meta,
            problems=problem_set,
            envs=env_set,
            categories=category_set,
            session_ids=session_id_set,
            agent_types=agent_type_set,
            models=model_set,
        ):
            continue

        if not is_finished_session(run_meta):
            continue

        missing = missing_summary_artifacts(session_dir)
        if missing:
            continue

        selected.append(session_dir)

    eval_results = [
        build_eval_result_from_session_dir(session_dir) for session_dir in selected
    ]
    out_path = write_eval_summary_csv(
        eval_results, output_path or default_summary_csv_path(results_dir)
    )
    return out_path
