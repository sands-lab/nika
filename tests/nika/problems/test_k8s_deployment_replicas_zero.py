"""Explicit Deployment targeting for k8s_deployment_replicas_zero."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.problems.management_orchestration_plane import kubernetes as k8s_mgmt
from nika.problems.management_orchestration_plane.kubernetes import (
    DeploymentReplicasZero,
    DeploymentReplicasZeroParams,
    ReadinessProbeMisconfig,
)

pytestmark = pytest.mark.unit

_WEATHER_DEPLOY = {
    "kind": "Deployment",
    "metadata": {"name": "weather-dply", "namespace": "weather-ns"},
    "spec": {"template": {"metadata": {"labels": {"app": "weather"}}}, "replicas": 1},
}
_WORDDB_DEPLOY = {
    "kind": "Deployment",
    "metadata": {"name": "worddb-dply", "namespace": "word-ns"},
    "spec": {"template": {"metadata": {"labels": {"app": "worddb"}}}, "replicas": 1},
}
_WEATHER_SVC = {
    "metadata": {"name": "weather", "namespace": "weather-ns"},
    "spec": {"selector": {"app": "weather"}},
}
_WORDDB_SVC = {
    "metadata": {"name": "worddb", "namespace": "word-ns"},
    "spec": {"selector": {"app": "worddb"}},
}
_WORD_BACKEND = ("word-ns", "word", "http://datacenter.com/word")
_WEATHER_BACKEND = ("weather-ns", "weather", "http://datacenter.com/weather")


class _K8s:
    def __init__(self, commands: dict[str, object]) -> None:
        self.commands = commands

    def kubectl_json(self, control: str, cmd: str):
        del control
        try:
            return self.commands[cmd]
        except KeyError as exc:
            raise AssertionError(f"unexpected kubectl {cmd!r}") from exc


@pytest.mark.parametrize(
    "problem_class", [DeploymentReplicasZero, ReadinessProbeMisconfig]
)
def test_explicit_deployment_uses_its_service_not_first_ingress(
    monkeypatch, problem_class
) -> None:
    monkeypatch.setattr(
        k8s_mgmt,
        "traffic_backends",
        lambda *_: [_WORD_BACKEND, _WEATHER_BACKEND],
    )
    k8s = _K8s(
        {
            "get deploy weather-dply -n weather-ns": _WEATHER_DEPLOY,
            "get svc -n weather-ns": {"items": [_WEATHER_SVC]},
        }
    )
    problem = object.__new__(problem_class)
    problem.runtime = SimpleNamespace(lab_api=k8s)
    problem.k8s_control_node = "controller"
    params = DeploymentReplicasZeroParams(
        deployment_name="weather-dply", namespace="weather-ns"
    )
    assert problem._targets(params) == (
        "weather-ns",
        "weather-dply",
        "weather-ns",
        "weather",
    )


@pytest.mark.parametrize(
    "problem_class", [DeploymentReplicasZero, ReadinessProbeMisconfig]
)
def test_explicit_deployment_without_traffic_path_is_rejected(
    monkeypatch, problem_class
) -> None:
    monkeypatch.setattr(k8s_mgmt, "traffic_backends", lambda *_: [_WORD_BACKEND])
    k8s = _K8s(
        {
            "get deploy worddb-dply -n word-ns": _WORDDB_DEPLOY,
            "get svc -n word-ns": {"items": [_WORDDB_SVC]},
        }
    )
    problem = object.__new__(problem_class)
    problem.runtime = SimpleNamespace(lab_api=k8s)
    problem.k8s_control_node = "controller"
    params = DeploymentReplicasZeroParams(
        deployment_name="worddb-dply", namespace="word-ns"
    )
    with pytest.raises(ValueError, match="no Service on"):
        problem._targets(params)


@pytest.mark.parametrize("pods", [0, 1])
def test_zero_replica_symptom_waits_for_pods_to_drain(monkeypatch, pods):
    from nika.validation.symptom import k8s_probes as probes

    problem = SimpleNamespace(
        service_ref="business/app",
        deployment_ref="business/deploy",
        root_cause_name="k8s_deployment_replicas_zero",
        _audit_service_codes={"business/app": "200"},
        _audit_service_probes={
            "business/app": probes.HttpProbeRequest(url="http://app/")
        },
    )
    monkeypatch.setattr(probes, "_client", lambda _: "client")
    monkeypatch.setattr(probes, "_http_exchange", lambda *_: ("503", ""))
    monkeypatch.setattr(
        probes,
        "_deployment_sample",
        lambda _: {"replicas": 0, "pods": pods, "ready_pods": 0},
    )
    monkeypatch.setattr(probes, "_wait_for", lambda check: check())
    ok, _ = probes.replicas_zero_broken(problem, None)
    assert ok is (pods == 0)
