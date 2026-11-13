"""Target unavailability is distinct from broken case generation."""

import pytest

from nika.problems.support.benchmark_targets import NoValidTargetError
from nika.workflows.benchmark import generate
from nika.workflows.benchmark.inject_resolve import (
    resolve_multi_inject_params,
    validate_benchmark_case,
)

pytestmark = pytest.mark.contract


def _build(monkeypatch, error):
    def enumerate_targets(*args, **kwargs):
        raise error

    monkeypatch.setattr(generate, "enumerate_inject_params", enumerate_targets)
    return generate.build_failure_cases(
        problem="link_down", scenario="dc_clos", topo_size="s", isp_options=None
    )


def test_no_valid_target_is_an_explicit_rejection(monkeypatch):
    cases, rejected = _build(monkeypatch, NoValidTargetError("no eligible link"))
    assert cases == []
    assert len(rejected) == 1
    assert "no eligible link" in rejected[0]["error"]


def test_generator_errors_propagate(monkeypatch):
    error = ValueError("invalid generated configuration")
    with pytest.raises(RuntimeError, match="Cannot enumerate targets") as result:
        _build(monkeypatch, error)
    assert result.value.__cause__ is error


def test_pmtu_combination_selects_eligible_native_deployment_targets():
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.support.router_ops import native_router_profile

    problems = ["mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"]
    options = {"device_profile": "iosxr"}
    env = load_offline_net_env("campus_lan", "s", **options)
    for seed in range(10):
        injects = resolve_multi_inject_params(
            problems, "campus_lan", "s", seed=seed, isp_options=options
        )
        host = injects["mtu_mismatch"]["host_name"]
        assert native_router_profile(env, host) != "iosxr"
        assert injects[problems[1]]["host_name"] == host
        for name, inject in injects.items():
            validate_benchmark_case(
                "campus_lan", name, inject, "s", isp_options=options, net_env=env
            )
