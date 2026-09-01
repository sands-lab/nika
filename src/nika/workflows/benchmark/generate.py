"""Generate the deterministic NIKA executable benchmark candidate catalog."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from nika.config import BENCHMARK_DIR
from nika.net_env.net_env_pool import (
    list_all_net_envs,
    scenario_fixed_topo_size,
    scenario_requires_topo_size,
    scenario_supported_backends,
)
from nika.problems.rca import UnresolvedRootCauseError
from nika.problems.rca.inventory import load_offline_net_env
from nika.problems.rca.materialize import ground_truth_for_case
from nika.problems.rca.models import FaultResource
from nika.problems.registry import list_avail_problem_instances
from nika.workflows.benchmark.e2e_validation import containerlab_isp_supported
from nika.workflows.benchmark.healthy import HEALTHY_PROBLEM
from nika.workflows.benchmark.inject_enumerate import enumerate_inject_params
from nika.workflows.benchmark.inject_resolve import validate_benchmark_case
from nika.workflows.benchmark.isp_options import (
    is_isp_base_topology,
    is_isp_named_special,
    isp_config_for_problem,
    isp_stack_for_backend,
)
from nika.workflows.benchmark.resume import benchmark_option_id

EXCLUDED_SCENARIOS = frozenset({"iosxr_simple_bgp"})
WORKING_DIRNAME = "working"
POOL_DIRNAME = "pool"


def default_pool_dir() -> Path:
    return BENCHMARK_DIR / WORKING_DIRNAME / POOL_DIRNAME


_PROFILE_FIELDS = (
    "topo_size",
    "igp",
    "bgp_mode",
    "rpki",
    "backend",
    "device_profile",
)
_ISP_DEPLOY_KEYS = ("igp", "bgp_mode", "rpki", "backend", "device_profile")


def _topo_sizes_for_scenario(scenario: str) -> list[str]:
    return ["s", "m", "l"] if scenario_requires_topo_size(scenario) else [""]


def _fixed_or_empty_topo_size(scenario: str) -> str:
    return scenario_fixed_topo_size(scenario) or ""


def _profile_fields(
    topo_size: str, isp_options: dict[str, Any] | None
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if topo_size:
        fields["topo_size"] = topo_size
    if isp_options:
        fields.update(isp_options)
    return fields


def _variant_sort_key(variant: dict[str, Any]) -> tuple[str, ...]:
    size_order = {"": "0", "s": "1", "m": "2", "l": "3"}
    return (
        size_order.get(str(variant.get("topo_size") or ""), "9"),
        str(variant.get("igp") or ""),
        str(variant.get("bgp_mode") or ""),
        str(bool(variant.get("rpki", False))),
        str(variant.get("backend") or ""),
        str(variant.get("device_profile") or ""),
    )


def _problem_catalog_backends(problem_cls: type) -> tuple[str, ...]:
    """Backends to expand in the ISP catalog.

    Undeclared ``supported_backends`` means Kathara-only for ISP expansion so
    undeclared SRL-incompatible faults are not duplicated onto Containerlab.
    """
    supported = getattr(problem_cls, "supported_backends", None)
    if supported is None:
        return ("kathara",)
    return tuple(str(item) for item in supported)


def _isp_stack_variants(
    scenario: str, problem_cls: type | None
) -> list[dict[str, str]]:
    scenario_backends = set(scenario_supported_backends(scenario))
    if is_isp_named_special(scenario):
        if "kathara" not in scenario_backends:
            return []
        return [isp_stack_for_backend("kathara")]

    if problem_cls is None:
        problem_backends = {"kathara", "containerlab"}
    else:
        problem_backends = set(_problem_catalog_backends(problem_cls))
    allowed = scenario_backends & problem_backends
    variants: list[dict[str, str]] = []
    if "kathara" in allowed:
        variants.append(isp_stack_for_backend("kathara"))
    if "containerlab" in allowed and containerlab_isp_supported(scenario):
        variants.append(isp_stack_for_backend("containerlab"))
    if not variants and "kathara" in scenario_backends:
        variants.append(isp_stack_for_backend("kathara"))
    return variants


def _offline_kwargs(isp_options: dict[str, Any] | None) -> dict[str, Any]:
    if not isp_options:
        return {}
    return {key: isp_options[key] for key in _ISP_DEPLOY_KEYS if key in isp_options}


def _document_sort_key(document: dict[str, Any]) -> tuple[str, ...]:
    scenario = str(document["scenario"]["name"])
    if "failure" in document:
        return (scenario, "0", str(document["failure"]["fault_type"]))
    return (scenario, "1", "healthy")


def _normalized_inject(params: dict[str, Any]) -> dict[str, str]:
    return {str(key): str(value) for key, value in sorted(params.items())}


def _inject_key(params: dict[str, str]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def _option_id(
    *,
    scenario: str,
    topo_size: str,
    problem: str | None,
    inject: dict[str, str],
    isp_options: dict[str, Any] | None,
) -> str:
    row: dict[str, Any] = {
        "scenario": scenario,
        "topo_size": topo_size,
        "problem": problem or HEALTHY_PROBLEM,
        "inject": inject,
    }
    row.update(isp_options or {})
    return benchmark_option_id(row)


def _catalog_root_causes(
    root_causes: list[Any], *, fault_type: str
) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for root_cause in root_causes:
        resource = FaultResource.model_validate(root_cause.resource)
        catalog.append(
            {
                "resource": resource.model_dump(
                    mode="json", exclude_none=True, exclude={"id"}
                ),
                "fault_type": fault_type,
            }
        )
    return catalog


def _failure_group_specs() -> Iterable[tuple[str, str, str, dict[str, Any] | None]]:
    net_envs = list_all_net_envs()
    for problem, problem_cls in sorted(list_avail_problem_instances().items()):
        problem_tags = set(problem_cls.TAGS)
        for scenario, scenario_spec in sorted(net_envs.items()):
            if scenario in EXCLUDED_SCENARIOS:
                continue
            if not problem_tags.issubset(set(scenario_spec.TAGS)):
                continue
            allowed = problem_cls.compatible_scenarios()
            if allowed is not None and scenario not in allowed:
                continue
            if is_isp_named_special(scenario):
                for stack in _isp_stack_variants(scenario, problem_cls):
                    yield (
                        problem,
                        scenario,
                        _fixed_or_empty_topo_size(scenario),
                        dict(stack),
                    )
                continue
            if is_isp_base_topology(scenario):
                # RPKI only on named RPKI scenarios (handled above via tags).
                if problem == "bgp_rpki_invalid_route_leak":
                    continue
                protocol = isp_config_for_problem(problem, problem_tags)
                for stack in _isp_stack_variants(scenario, problem_cls):
                    yield (
                        problem,
                        scenario,
                        _fixed_or_empty_topo_size(scenario),
                        {**protocol, **stack},
                    )
                continue
            for topo_size in _topo_sizes_for_scenario(scenario):
                yield problem, scenario, topo_size, None


def _rejection(
    *,
    reason: str,
    problem: str,
    scenario: str,
    topo_size: str,
    inject: dict[str, str] | None,
    error: BaseException | str,
    isp_options: dict[str, Any] | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "reason": reason,
        "problem": problem,
        "scenario": scenario,
        "topo_size": topo_size or None,
        "inject": inject or {},
        "error": str(error),
    }
    if isp_options:
        row.update(isp_options)
    return row


def _case_sort_key(case: dict[str, Any]) -> tuple[str, ...]:
    return _variant_sort_key(case) + (_inject_key(case["inject"]),)


def _build_failure_cases(
    *, problem: str, scenario: str, topo_size: str, isp_options: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    collected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    net_env = load_offline_net_env(scenario, topo_size, **_offline_kwargs(isp_options))
    try:
        enumerated = enumerate_inject_params(
            problem,
            scenario,
            topo_size,
            isp_options=isp_options,
            net_env=net_env,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Cannot enumerate targets for {problem}/{scenario}/{topo_size or '-'}"
        ) from exc

    seen_inject: set[str] = set()
    seen_ids: set[str] = set()
    for raw in enumerated:
        inject = _normalized_inject(raw)
        inject_key = _inject_key(inject)
        if inject_key in seen_inject:
            rejected.append(
                _rejection(
                    reason="duplicate_normalized_target",
                    problem=problem,
                    scenario=scenario,
                    topo_size=topo_size,
                    inject=inject,
                    error="duplicate normalized inject parameters",
                    isp_options=isp_options,
                )
            )
            continue
        seen_inject.add(inject_key)
        try:
            validate_benchmark_case(
                scenario,
                problem,
                inject,
                topo_size,
                isp_options=isp_options,
                net_env=net_env,
            )
        except ValueError as exc:
            rejected.append(
                _rejection(
                    reason="case_validation_failed",
                    problem=problem,
                    scenario=scenario,
                    topo_size=topo_size,
                    inject=inject,
                    error=exc,
                    isp_options=isp_options,
                )
            )
            continue
        try:
            truth = ground_truth_for_case(
                problem=problem,
                params=inject,
                scenario=scenario,
                topo_size=topo_size,
                net_env=net_env,
            )
            catalog_root_causes = _catalog_root_causes(
                truth.root_causes, fault_type=problem
            )
        except UnresolvedRootCauseError as exc:
            rejected.append(
                _rejection(
                    reason="ground_truth_unresolved",
                    problem=problem,
                    scenario=scenario,
                    topo_size=topo_size,
                    inject=inject,
                    error=exc,
                    isp_options=isp_options,
                )
            )
            continue
        except ValueError as exc:
            rejected.append(
                _rejection(
                    reason="prerequisite_not_satisfied",
                    problem=problem,
                    scenario=scenario,
                    topo_size=topo_size,
                    inject=inject,
                    error=exc,
                    isp_options=isp_options,
                )
            )
            continue
        option_id = _option_id(
            scenario=scenario,
            topo_size=topo_size,
            problem=problem,
            inject=inject,
            isp_options=isp_options,
        )
        if option_id in seen_ids:
            raise RuntimeError(
                f"Duplicate option_id {option_id} in {problem}/{scenario}/{topo_size or '-'}"
            )
        seen_ids.add(option_id)
        collected.append(
            {
                **_profile_fields(topo_size, isp_options),
                "inject": inject,
                "root_causes": catalog_root_causes,
            }
        )

    if not collected:
        if not rejected:
            rejected.append(
                _rejection(
                    reason="invalid_target",
                    problem=problem,
                    scenario=scenario,
                    topo_size=topo_size,
                    inject=None,
                    error="semantic enumerator found no legal inject targets",
                    isp_options=isp_options,
                )
            )
        return [], rejected
    collected.sort(key=_case_sort_key)
    return collected, rejected


def _healthy_specs(
    failure_documents: Iterable[dict[str, Any]],
) -> Iterable[tuple[str, str, dict[str, Any] | None]]:
    seen: set[tuple[Any, ...]] = set()

    def _emit(
        scenario: str,
        topo_size: str,
        isp_options: dict[str, Any] | None,
    ) -> tuple[str, str, dict[str, Any] | None] | None:
        opts = isp_options or {}
        key = (
            scenario,
            topo_size,
            str(opts.get("igp") or ""),
            str(opts.get("bgp_mode") or ""),
            bool(opts.get("rpki", False)),
            str(opts.get("backend") or ""),
            str(opts.get("device_profile") or ""),
        )
        if key in seen:
            return None
        seen.add(key)
        return scenario, topo_size, isp_options

    for scenario in sorted(list_all_net_envs()):
        if scenario in EXCLUDED_SCENARIOS:
            continue
        if is_isp_base_topology(scenario):
            protocol = {"igp": "ospf", "bgp_mode": "ebgp", "rpki": False}
            for stack in _isp_stack_variants(scenario, None):
                spec = _emit(
                    scenario,
                    _fixed_or_empty_topo_size(scenario),
                    {**protocol, **stack},
                )
                if spec is not None:
                    yield spec
            continue
        if is_isp_named_special(scenario):
            for stack in _isp_stack_variants(scenario, None):
                spec = _emit(scenario, _fixed_or_empty_topo_size(scenario), dict(stack))
                if spec is not None:
                    yield spec
            continue
        for topo_size in _topo_sizes_for_scenario(scenario):
            spec = _emit(scenario, topo_size, None)
            if spec is not None:
                yield spec

    for document in sorted(failure_documents, key=_document_sort_key):
        scenario = str(document["scenario"]["name"])
        for case in document["failure"]["cases"]:
            topo_size = str(case.get("topo_size") or "")
            isp_options = {
                key: case[key] for key in _ISP_DEPLOY_KEYS if key in case
            } or None
            spec = _emit(scenario, topo_size, isp_options)
            if spec is not None:
                yield spec


def _document_option_ids(document: dict[str, Any]) -> list[str]:
    if "failure" in document:
        ids: list[str] = []
        for case in document["failure"]["cases"]:
            row = {
                "scenario": document["scenario"]["name"],
                "problem": document["failure"]["fault_type"],
                "inject": case["inject"],
                **{key: case[key] for key in _PROFILE_FIELDS if key in case},
            }
            ids.append(benchmark_option_id(row))
        return ids
    ids = []
    for variant in document["healthy"]["variants"]:
        row = {
            "scenario": document["scenario"]["name"],
            "problem": HEALTHY_PROBLEM,
            "inject": {},
            **{key: variant[key] for key in _PROFILE_FIELDS if key in variant},
        }
        ids.append(benchmark_option_id(row))
    return ids


def _document_path(document: dict[str, Any]) -> Path:
    scenario = str(document["scenario"]["name"])
    leaf = (
        f"{document['failure']['fault_type']}.yaml"
        if "failure" in document
        else "healthy.yaml"
    )
    return Path(scenario) / leaf


def build_candidate_catalog() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    failure_docs: dict[tuple[str, str], dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    all_ids: set[str] = set()

    for problem, scenario, topo_size, isp_options in _failure_group_specs():
        cases, case_rejected = _build_failure_cases(
            problem=problem,
            scenario=scenario,
            topo_size=topo_size,
            isp_options=isp_options,
        )
        rejected.extend(case_rejected)
        if not cases:
            continue
        key = (scenario, problem)
        document = failure_docs.get(key)
        if document is None:
            document = {
                "scenario": {"name": scenario},
                "failure": {"fault_type": problem, "cases": []},
            }
            failure_docs[key] = document
        document["failure"]["cases"].extend(cases)

    documents: list[dict[str, Any]] = []
    for document in failure_docs.values():
        document["failure"]["cases"].sort(key=_case_sort_key)
        for option_id in _document_option_ids(document):
            if option_id in all_ids:
                raise RuntimeError(f"Duplicate catalog option_id {option_id}")
            all_ids.add(option_id)
        documents.append(document)

    healthy_docs: dict[str, dict[str, Any]] = {}
    for scenario, topo_size, isp_options in _healthy_specs(failure_docs.values()):
        variant = _profile_fields(topo_size, isp_options)
        document = healthy_docs.get(scenario)
        if document is None:
            document = {
                "scenario": {"name": scenario},
                "healthy": {"variants": []},
            }
            healthy_docs[scenario] = document
        document["healthy"]["variants"].append(variant)

    for document in healthy_docs.values():
        document["healthy"]["variants"].sort(key=_variant_sort_key)
        for option_id in _document_option_ids(document):
            if option_id in all_ids:
                raise RuntimeError(f"Duplicate catalog option_id {option_id}")
            all_ids.add(option_id)
        documents.append(document)

    documents.sort(key=_document_sort_key)
    return documents, _build_report(documents, rejected)


def _build_report(
    documents: list[dict[str, Any]], rejected: list[dict[str, Any]]
) -> dict[str, Any]:
    failure_groups = [row for row in documents if "failure" in row]
    healthy = [row for row in documents if "healthy" in row]
    by_failure_scenarios: dict[str, set[str]] = defaultdict(set)
    by_failure_options: Counter[str] = Counter()
    by_scenario_scale: Counter[str] = Counter()
    for document in failure_groups:
        scenario = str(document["scenario"]["name"])
        problem = str(document["failure"]["fault_type"])
        cases = document["failure"]["cases"]
        by_failure_scenarios[problem].add(scenario)
        by_failure_options[problem] += len(cases)
        for case in cases:
            size = str(case.get("topo_size") or "-")
            by_scenario_scale[f"{scenario}/{size}"] += 1
    return {
        "summary": {
            "candidate_files": len(documents),
            "failure_candidate_files": len(failure_groups),
            "concrete_inject_options": sum(by_failure_options.values()),
            "failures": len(by_failure_options),
            "scenarios": len({str(row["scenario"]["name"]) for row in documents}),
            "healthy_candidate_files": len(healthy),
            "validation_rejected_options": len(rejected),
        },
        "failure_scenario_counts": {
            problem: len(scenarios)
            for problem, scenarios in sorted(by_failure_scenarios.items())
        },
        "failure_option_counts": dict(sorted(by_failure_options.items())),
        "scenario_scale_option_counts": dict(sorted(by_scenario_scale.items())),
        "rejection_reason_counts": dict(
            sorted(Counter(row["reason"] for row in rejected).items())
        ),
        "rejected": rejected,
    }


def _print_report(report: dict[str, Any]) -> None:
    print("benchmark candidate catalog")
    for key, value in report["summary"].items():
        print(f"  {key}: {value}")
    print("failure scenario/options")
    for problem, options in report["failure_option_counts"].items():
        scenarios = report["failure_scenario_counts"][problem]
        print(f"  {problem}: scenarios={scenarios} options={options}")
    print("scenario/scale options")
    for key, count in report["scenario_scale_option_counts"].items():
        print(f"  {key}: {count}")
    if report["rejection_reason_counts"]:
        print("rejections")
        for reason, count in report["rejection_reason_counts"].items():
            print(f"  {reason}: {count}")


def _write_yaml_atomic(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(
        yaml.dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    tmp_path.replace(path)


def generate_candidate_catalog(
    *, out_path: Path | None = None
) -> tuple[Path, dict[str, Any]]:
    documents, report = build_candidate_catalog()
    catalog_dir = out_path or default_pool_dir()
    catalog_dir.mkdir(parents=True, exist_ok=True)
    expected_paths: set[Path] = set()
    for document in documents:
        relative_path = _document_path(document)
        path = catalog_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_yaml_atomic(path, document)
        expected_paths.add(path)
    for path in catalog_dir.rglob("*.yaml"):
        if path not in expected_paths:
            path.unlink()
    for path in sorted(catalog_dir.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    _print_report(report)
    print(f"Wrote {catalog_dir}")
    return catalog_dir, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Pool directory (default: {default_pool_dir()})",
    )
    args = parser.parse_args(argv)
    generate_candidate_catalog(out_path=args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
