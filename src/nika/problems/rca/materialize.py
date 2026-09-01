"""Offline ground-truth helpers used by catalog generation and migrate."""

from __future__ import annotations

from typing import Any

from nika.problems.rca.models import (
    ProblemGroundTruth,
    RootCause,
    UnresolvedRootCauseError,
    canonical_root_causes,
)


def build_ground_truth(
    problem: Any,
    params: Any = None,
    net_env: Any = None,
) -> ProblemGroundTruth:
    """Attach env/params and call the failure's ``get_ground_truth()``."""
    if net_env is not None:
        problem.net_env = net_env
    if params is not None and hasattr(problem, "parse_params"):
        problem.parse_params(params)
    elif params is not None:
        problem._resolved_params = params
    return problem.get_ground_truth()


def build_multi_ground_truth(
    sub_faults: list[Any],
    *,
    failure_domain: str,
) -> ProblemGroundTruth:
    root_causes: list[RootCause] = []
    for fault in sub_faults:
        params = getattr(fault, "_resolved_params", None)
        piece = build_ground_truth(fault, params, getattr(fault, "net_env", None))
        root_causes.extend(piece.root_causes)
    return ProblemGroundTruth(
        is_anomaly=True,
        root_causes=root_causes,
        failure_domain=failure_domain,
    )


def ground_truth_for_multi_case(
    *,
    problems: list[str],
    params: dict[str, dict[str, str]],
    scenario: str,
    topo_size: str = "",
    net_env: Any = None,
    topo: str | None = None,
    igp: str | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
) -> ProblemGroundTruth:
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.registry import get_problem_class

    env = net_env
    if env is None:
        env = load_offline_net_env(
            scenario,
            topo_size,
            topo=topo,
            igp=igp,
            bgp_mode=bgp_mode,
            rpki=rpki,
        )
    sub_faults: list[Any] = []
    for problem in problems:
        cls = get_problem_class(problem)
        if cls is None:
            raise UnresolvedRootCauseError(f"Unknown failure {problem!r}.")
        instance = cls.__new__(cls)
        instance.root_cause_name = cls.root_cause_name
        instance.symptom_desc = getattr(cls, "symptom_desc", "") or ""
        instance.net_env = env
        instance.scenario_name = scenario
        instance._resolved_params = None
        instance.parse_params(params[problem])
        sub_faults.append(instance)
    return build_multi_ground_truth(sub_faults, failure_domain="multiple_faults")


def ground_truth_for_case(
    *,
    problem: str,
    params: Any,
    scenario: str,
    topo_size: str = "",
    net_env: Any = None,
) -> ProblemGroundTruth:
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.registry import get_problem_class

    cls = get_problem_class(problem)
    if cls is None:
        raise UnresolvedRootCauseError(f"Unknown failure {problem!r}.")
    env = net_env if net_env is not None else load_offline_net_env(scenario, topo_size)
    instance = cls.__new__(cls)
    instance.root_cause_name = cls.root_cause_name
    instance.symptom_desc = getattr(cls, "symptom_desc", "") or ""
    instance.net_env = env
    instance.scenario_name = scenario
    instance._resolved_params = None
    instance.parse_params(params)
    return instance.get_ground_truth()


def assert_root_causes_match(actual: ProblemGroundTruth | dict, expected: list) -> None:
    left = canonical_root_causes(
        actual.root_causes
        if isinstance(actual, ProblemGroundTruth)
        else actual.get("root_causes") or []
    )
    right = canonical_root_causes(expected)
    if left != right:
        raise ValueError(
            "Injected ground truth does not match materialized benchmark "
            f"root_causes.\nactual={left}\nexpected={right}"
        )
