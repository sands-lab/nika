"""Kubernetes environment briefs must not reveal whether a fault is present."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.registry import get_problem_class, list_avail_problem_instances
from nika.workflows.benchmark.healthy import healthy_task_description

pytestmark = pytest.mark.contract

_K8S_LABS = ("k8s_lab", "llmd_lab")
_FAULTS = ("k8s_coredns_isolated", "k8s_networkpolicy_deny", "link_down")


@pytest.mark.parametrize("scenario", _K8S_LABS)
@pytest.mark.parametrize("problem_name", _FAULTS)
def test_k8s_fault_prompt_matches_healthy(scenario: str, problem_name: str) -> None:
    env = get_net_env_instance(scenario)
    expected = healthy_task_description(env)
    assert "`kubectl` works only on controller" in expected
    assert "faulty" not in expected.lower()
    cls = get_problem_class(problem_name)
    assert cls is not None
    problem = cls(None)
    problem.net_env = env
    assert problem.get_task_description() == expected


@pytest.mark.parametrize(
    "scenario,size",
    [(scenario, None) for scenario in _K8S_LABS]
    + [
        (scenario, size)
        for scenario in (
            "dc_clos",
            "campus_lan",
            "enterprise_branch",
            "sdn_l3_clos",
            "p4_dc_gateway",
        )
        for size in ("s", "m", "l")
    ],
)
def test_registered_fault_briefs_match_the_healthy_variant(scenario, size) -> None:
    env = get_net_env_instance(scenario, **({"topo_size": size} if size else {}))
    expected = healthy_task_description(env)
    for name, cls in list_avail_problem_instances().items():
        problem = cls(None)
        problem.net_env = env
        assert problem.get_task_description() == expected, (scenario, size, name)
