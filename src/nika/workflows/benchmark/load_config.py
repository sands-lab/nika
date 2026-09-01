"""Load benchmark case definitions from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from nika.net_env.net_env_pool import resolve_scenario_id, scenario_fixed_topo_size
from nika.problems.registry import list_avail_problem_instances, resolve_problem_name
from nika.workflows.benchmark.healthy import HEALTHY_PROBLEM, is_healthy_case
from nika.workflows.benchmark.isp_options import (
    is_isp_base_topology,
    is_isp_scenario,
    validate_and_resolve_isp_options,
)
from nika.workflows.benchmark.multi_fault import join_problem_label
from nika.workflows.benchmark.resume import benchmark_option_id


def _row_problem_fields(row: dict[str, Any]) -> tuple[list[str], str]:
    raw_problems = row.get("problems")
    if raw_problems is not None:
        if not isinstance(raw_problems, list) or not raw_problems:
            raise ValueError("'problems' must be a non-empty list")
        problems = [resolve_problem_name(str(item)) for item in raw_problems]
        return problems, join_problem_label(problems)
    raw_problem = str(row["problem"])
    problem = resolve_problem_name(raw_problem)
    return [problem], problem


def normalize_benchmark_row(row: dict[str, Any]) -> dict[str, Any]:
    """Validate a canonical benchmark row and normalize its failure options."""
    raw_scenario = str(row["scenario"])
    canonical = resolve_scenario_id(raw_scenario)
    if "workload" in row:
        raise ValueError("Benchmark cases do not accept a workload field.")

    topo = row.get("topo_size")
    if topo is None:
        topo = ""
    topo_size = "" if topo in ("-", "", None) else str(topo)

    problems, problem_label = _row_problem_fields(row)
    raw_problem = str(row.get("problem") or join_problem_label(problems))
    inject = row.get("inject") or {}
    if not isinstance(inject, dict):
        raise ValueError("'inject' must be a mapping")

    isp_topo = row.get("topo")
    isp_igp = row.get("igp")
    isp_bgp = row.get("bgp_mode")
    isp_rpki = row.get("rpki")
    isp_backend = row.get("backend")
    isp_device_profile = row.get("device_profile")

    if is_healthy_case(raw_problem):
        if inject:
            raise ValueError(
                f"Case {canonical}/{HEALTHY_PROBLEM} must use an empty 'inject' map"
            )
        root_causes = row.get("root_causes")
        if root_causes:
            raise ValueError(
                f"Case {canonical}/{HEALTHY_PROBLEM} must not declare root_causes"
            )
        if is_isp_base_topology(canonical):
            missing = [
                key
                for key, value in (
                    ("igp", isp_igp),
                    ("bgp_mode", isp_bgp),
                    ("rpki", isp_rpki),
                    ("backend", isp_backend),
                    ("device_profile", isp_device_profile),
                )
                if value in (None, "", "-")
            ]
            if missing:
                raise ValueError(
                    f"Healthy ISP case requires explicit deploy options; "
                    f"missing {missing}"
                )
        isp_options = validate_and_resolve_isp_options(
            scenario=canonical,
            problem=HEALTHY_PROBLEM,
            problem_tags=set(),
            topo_size=topo_size,
            topo=isp_topo,
            igp=isp_igp,
            bgp_mode=isp_bgp,
            rpki=isp_rpki,
            backend=isp_backend,
            device_profile=isp_device_profile,
        )
        if is_isp_scenario(canonical):
            fixed = scenario_fixed_topo_size(canonical)
            if fixed:
                if not topo_size:
                    topo_size = fixed
                elif topo_size != fixed:
                    raise ValueError(
                        f"Scenario {canonical!r} has fixed topo_size {fixed!r}; "
                        f"got {topo_size!r}."
                    )
        normalized: dict[str, Any] = {
            "scenario": canonical,
            "problem": HEALTHY_PROBLEM,
            "topo_size": topo_size,
            "inject": {},
            "root_causes": [],
        }
        if isp_options is not None:
            normalized.update(isp_options)
        return normalized

    problem = problem_label
    problem_tags: set[str] = set()
    for item in problems:
        problem_cls = list_avail_problem_instances().get(item)
        if problem_cls is not None:
            problem_tags.update(problem_cls.TAGS)
    isp_options = validate_and_resolve_isp_options(
        scenario=canonical,
        problem=problems[0],
        problem_tags=problem_tags,
        topo_size=topo_size,
        topo=isp_topo,
        igp=isp_igp,
        bgp_mode=isp_bgp,
        rpki=isp_rpki,
        backend=isp_backend,
        device_profile=isp_device_profile,
    )
    if is_isp_scenario(canonical):
        fixed = scenario_fixed_topo_size(canonical)
        if fixed:
            if not topo_size:
                topo_size = fixed
            elif topo_size != fixed:
                raise ValueError(
                    f"Scenario {canonical!r} has fixed topo_size {fixed!r}; "
                    f"got {topo_size!r}."
                )

    if not inject:
        raise ValueError(
            f"Case {canonical}/{raw_problem} missing non-empty 'inject' map"
        )
    if len(problems) > 1:
        nested: dict[str, dict[str, str]] = {}
        for item in problems:
            piece = inject.get(item)
            if not isinstance(piece, dict) or not piece:
                raise ValueError(
                    f"Multi-fault case must provide inject.{item} mapping"
                )
            nested[item] = {str(k): str(v) for k, v in piece.items()}
        inject = nested
    else:
        inject = {str(k): str(v) for k, v in inject.items()}
    root_causes = row.get("root_causes")
    if root_causes and raw_problem != problem:
        rewritten: list[Any] = []
        for entry in root_causes:
            if not isinstance(entry, dict):
                rewritten.append(entry)
                continue
            updated = dict(entry)
            if updated.get("fault_type") == raw_problem:
                updated["fault_type"] = problem
            rewritten.append(updated)
        root_causes = rewritten

    normalized = {
        "scenario": canonical,
        "problem": problem,
        "problems": problems,
        "topo_size": topo_size,
        "inject": inject,
    }
    if isp_options is not None:
        normalized.update(isp_options)
    if root_causes:
        normalized["root_causes"] = root_causes
    if row.get("root_causes_status"):
        normalized["root_causes_status"] = row["root_causes_status"]
    return normalized


def load_benchmark_yaml(path: str | Path) -> list[dict[str, Any]]:
    """Load benchmark cases from a YAML file.

    Expected shape::

        cases:
          - scenario: dc_clos
            topo_size: s
            problem: link_down
            inject:
              host_name: pc_0_0
              intf_name: eth0

    ISP base cases carry ``igp`` / ``bgp_mode`` / ``rpki`` deploy options (topology
    is baked into the scenario ID). Named ISP specials omit protocol fields.
    Healthy (no-fault) cases use ``problem: healthy`` with an empty ``inject`` map.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "cases" not in data:
        raise ValueError(f"Invalid benchmark YAML (missing top-level 'cases'): {path}")
    cases = data["cases"]
    if not isinstance(cases, list):
        raise ValueError(f"Invalid benchmark YAML ('cases' must be a list): {path}")
    normalized: list[dict[str, Any]] = []
    for idx, row in enumerate(cases):
        if not isinstance(row, dict):
            raise ValueError(f"Benchmark case {idx} must be a mapping")
        required = ("scenario",)
        for key in required:
            if key not in row:
                raise ValueError(f"Benchmark case {idx} missing required field {key!r}")
        if "problem" not in row and "problems" not in row:
            raise ValueError(
                f"Benchmark case {idx} missing required field 'problem' or 'problems'"
            )
        try:
            normalized.append(normalize_benchmark_row(row))
        except ValueError as exc:
            raise ValueError(f"Benchmark case {idx} ({path}): {exc}") from exc
    return normalized


_DEPLOY_FIELDS = _PROFILE_FIELDS = (
    "topo_size",
    "igp",
    "bgp_mode",
    "rpki",
    "backend",
    "device_profile",
)
_VARIANT_META = frozenset(_PROFILE_FIELDS)
_CASE_META = frozenset(_PROFILE_FIELDS) | {"inject", "root_causes"}


def _variant_common(scenario_name: str, variant: dict[str, Any]) -> dict[str, Any]:
    unknown = set(variant) - _VARIANT_META
    if unknown:
        raise ValueError(f"Unknown variant fields: {sorted(unknown)}")
    return {
        "scenario": scenario_name,
        **{key: variant[key] for key in _PROFILE_FIELDS if key in variant},
    }


def _case_common(scenario_name: str, case: dict[str, Any]) -> dict[str, Any]:
    unknown = set(case) - _CASE_META
    if unknown:
        raise ValueError(f"Unknown case fields: {sorted(unknown)}")
    return {
        "scenario": scenario_name,
        **{key: case[key] for key in _PROFILE_FIELDS if key in case},
    }


def _load_candidate_file(data: Any, path: str | Path) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError(f"Invalid candidate file: {path}")
    scenario = data.get("scenario")
    if not isinstance(scenario, dict) or not scenario.get("name"):
        raise ValueError(f"Candidate file requires scenario.name: {path}")
    if ("failure" in data) == ("healthy" in data):
        raise ValueError(
            f"Candidate file requires one failure or healthy section: {path}"
        )
    scenario_name = str(scenario["name"])
    expanded: list[dict[str, Any]] = []
    if "healthy" in data:
        healthy = data["healthy"]
        if not isinstance(healthy, dict) or set(healthy) != {"variants"}:
            raise ValueError(f"Invalid healthy candidate section: {path}")
        variants = healthy["variants"]
        if not isinstance(variants, list) or not variants:
            raise ValueError(f"Healthy candidate requires variants: {path}")
        for index, variant in enumerate(variants):
            location = f"Healthy variant {index} ({path})"
            if not isinstance(variant, dict):
                raise ValueError(f"{location} must be a mapping")
            expanded.append(
                {
                    **_variant_common(scenario_name, variant),
                    "problem": HEALTHY_PROBLEM,
                    "inject": {},
                    "root_causes": [],
                }
            )
    else:
        failure = data["failure"]
        if not isinstance(failure, dict) or set(failure) != {"fault_type", "cases"}:
            raise ValueError(f"Invalid failure candidate section: {path}")
        fault_type = str(failure["fault_type"] or "")
        cases = failure["cases"]
        if not fault_type or not isinstance(cases, list) or not cases:
            raise ValueError(f"Failure candidate requires fault_type and cases: {path}")
        for case_index, case in enumerate(cases):
            location = f"Failure case {case_index} ({path})"
            if not isinstance(case, dict):
                raise ValueError(f"{location} must be a mapping")
            inject = case.get("inject")
            root_causes = case.get("root_causes")
            if not isinstance(inject, dict) or not inject:
                raise ValueError(f"{location} requires non-empty inject mapping")
            if any(isinstance(value, list) for value in inject.values()):
                raise ValueError(f"{location} inject must use scalar values")
            if not isinstance(root_causes, list) or not root_causes:
                raise ValueError(f"{location} requires non-empty root_causes")
            expanded.append(
                {
                    **_case_common(scenario_name, case),
                    "problem": fault_type,
                    "inject": {str(key): str(value) for key, value in inject.items()},
                    "root_causes": root_causes,
                }
            )

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_row in expanded:
        try:
            normalized = normalize_benchmark_row(raw_row)
        except ValueError as exc:
            raise ValueError(f"Candidate row ({path}): {exc}") from exc
        option_id = benchmark_option_id(normalized)
        if option_id in seen_ids:
            raise ValueError(f"Duplicate candidate option {option_id!r}: {path}")
        seen_ids.add(option_id)
        normalized["candidate_option_id"] = option_id
        rows.append(normalized)
    return rows


def load_candidate_catalog(path: str | Path) -> list[dict[str, Any]]:
    """Validate and flatten every candidate YAML under a pool directory."""
    pool_dir = Path(path)
    if not pool_dir.is_dir():
        raise ValueError(f"Candidate pool must be a directory: {path}")
    files = sorted(pool_dir.rglob("*.yaml"))
    if not files:
        raise ValueError(f"Candidate pool has no YAML files: {path}")
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    root = pool_dir.resolve()
    for candidate_path in files:
        resolved = candidate_path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(
                f"Candidate file escapes pool directory: {candidate_path}"
            )
        candidate_data = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        for row in _load_candidate_file(candidate_data, resolved):
            option_id = row["candidate_option_id"]
            if option_id in seen_ids:
                raise ValueError(f"Duplicate candidate option_id {option_id!r}")
            seen_ids.add(option_id)
            rows.append(row)
    return rows


def load_benchmark_input(path: str | Path) -> list[dict[str, Any]]:
    """Load a flat case matrix, a single candidate file, or a pool directory."""
    target = Path(path)
    if target.is_dir():
        return load_candidate_catalog(target)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Invalid benchmark YAML (expected a mapping): {path}")
    if "candidate_files" in data:
        raise ValueError(
            f"candidate_files manifests are no longer supported; "
            f"pass a pool directory instead: {path}"
        )
    if "scenario" in data and ("failure" in data or "healthy" in data):
        return _load_candidate_file(data, target)
    return load_benchmark_yaml(target)
