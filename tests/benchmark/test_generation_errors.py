"""Target unavailability is distinct from broken case generation."""

import pytest

from nika.problems.support.benchmark_targets import NoValidTargetError
from nika.workflows.benchmark import generate

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
