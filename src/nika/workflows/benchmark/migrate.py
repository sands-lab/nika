"""Materialize structured root-cause labels on benchmark YAML cases."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from nika.problems.ground_truth import ground_truth_for_case
from nika.problems.root_cause import UnresolvedRootCauseError, canonical_root_causes
from nika.problems.topology_inventory import load_offline_net_env


def _load_raw(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "cases" not in data:
        raise ValueError(f"Invalid benchmark YAML (missing top-level 'cases'): {path}")
    return data


def _topo_size(row: dict[str, Any]) -> str:
    topo = row.get("topo_size") or ""
    if topo in ("-", None):
        return ""
    return str(topo)


def materialize_case(
    row: dict[str, Any],
    *,
    env_cache: dict[tuple[str, str], Any] | None = None,
) -> dict[str, Any]:
    """Copy identity fields and attach ``root_causes`` from the failure class."""
    if not isinstance(row, dict):
        raise ValueError("Benchmark case must be a mapping")
    scenario = str(row["scenario"])
    problem = str(row["problem"])
    topo = _topo_size(row)
    inject = {str(k): str(v) for k, v in dict(row.get("inject") or {}).items()}
    cache = env_cache if env_cache is not None else {}
    cache_key = (scenario, topo)
    if cache_key not in cache:
        cache[cache_key] = load_offline_net_env(scenario, topo)
    gt = ground_truth_for_case(
        problem=problem,
        params=inject,
        scenario=scenario,
        topo_size=topo,
        net_env=cache[cache_key],
    )
    return {
        "scenario": scenario,
        "topo_size": topo or None,
        "problem": problem,
        "inject": inject,
        "root_causes": canonical_root_causes(gt.root_causes),
    }


def materialize_cases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    env_cache: dict[tuple[str, str], Any] = {}
    return [materialize_case(row, env_cache=env_cache) for row in rows]


def write_cases_yaml(
    path: str | Path,
    *,
    seed: Any,
    cases: list[dict[str, Any]],
) -> None:
    Path(path).write_text(
        yaml.dump(
            {"seed": seed, "cases": cases},
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )


def migrate_benchmark_yaml(
    *,
    input_path: str | Path,
    output_path: str | Path,
    report_path: str | Path,
    allow_unresolved: bool = False,
) -> dict[str, Any]:
    src = Path(input_path)
    data = _load_raw(src)
    cases = list(data.get("cases") or [])
    env_cache: dict[tuple[str, str], Any] = {}
    unresolved: list[dict[str, Any]] = []
    migrated: list[dict[str, Any]] = []

    for index, row in enumerate(cases):
        if not isinstance(row, dict):
            raise ValueError(f"Benchmark case {index} must be a mapping")
        inject = dict(row.get("inject") or {})
        try:
            migrated.append(materialize_case(row, env_cache=env_cache))
        except UnresolvedRootCauseError as exc:
            failed = dict(row)
            failed.pop("schema_version", None)
            failed["root_causes_status"] = "unresolved"
            failed["root_causes_error"] = str(exc)
            migrated.append(failed)
            unresolved.append(
                {
                    "index": index,
                    "scenario": str(row.get("scenario")),
                    "problem": str(row.get("problem")),
                    "topo_size": _topo_size(row) or None,
                    "inject": inject,
                    "reason": str(exc),
                }
            )

    report = {
        "input": str(src),
        "output": str(output_path),
        "case_count": len(cases),
        "resolved": len(cases) - len(unresolved),
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
    }
    Path(report_path).write_text(
        yaml.dump(report, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    write_cases_yaml(output_path, seed=data.get("seed"), cases=migrated)
    if unresolved and not allow_unresolved:
        raise UnresolvedRootCauseError(
            f"{len(unresolved)} case(s) could not be migrated; see {report_path}"
        )
    return report
