"""Oracle evidence must distinguish failures with the same empty-Service symptom."""

from __future__ import annotations

import copy
import io
import json
import shlex
import subprocess
import sys

import pytest

from nika.problems.management_orchestration_plane.kubernetes import (
    DeploymentReplicasZero,
    NodeNoExecuteTaint,
    ReadinessProbeMisconfig,
)
from nika.problems.service_networking.kubernetes import (
    IngressClassMismatch,
    LoadBalancerAdvertisementMissing,
    ServiceSelectorMismatch,
    ServiceTargetPortMismatch,
)

pytestmark = pytest.mark.unit


def _inventory():
    return [
        {
            "kind": "Service",
            "metadata": {"name": "app", "namespace": "business"},
            "spec": {
                "selector": {"app": "app"},
                "ports": [{"port": 80, "targetPort": 8080}],
            },
        },
        {
            "kind": "Deployment",
            "metadata": {"name": "app", "namespace": "business", "generation": 2},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "app"}},
                "template": {"metadata": {"labels": {"app": "app"}}},
            },
            "status": {"observedGeneration": 2, "replicas": 1},
        },
        {
            "kind": "Pod",
            "metadata": {
                "name": "app-1",
                "namespace": "business",
                "uid": "pod-uid",
                "labels": {"app": "app"},
            },
            "spec": {"containers": [{"ports": [{"containerPort": 8080}]}]},
            "status": {
                "phase": "Running",
                "podIP": "10.42.1.2",
                "conditions": [{"type": "Ready", "status": "True"}],
                "containerStatuses": [
                    {
                        "ready": True,
                        "state": {"running": {"startedAt": "2026-11-02T00:00:00Z"}},
                    }
                ],
            },
        },
        {
            "kind": "EndpointSlice",
            "metadata": {
                "name": "app-ep",
                "namespace": "business",
                "labels": {"kubernetes.io/service-name": "app"},
            },
            "ports": [{"port": 8080}],
            "endpoints": [{"addresses": ["10.42.1.2"], "conditions": {"ready": True}}],
        },
    ]


def _check(cls, **overrides):
    problem = cls(None)
    params = cls.Params(control_node="controller", **overrides)
    return problem.oracle_playbook(params).steps[0].expect


@pytest.mark.parametrize(
    "fault", ["healthy", "port", "selector", "replicas", "readiness"]
)
def test_service_and_workload_faults_require_distinct_evidence(fault):
    items = _inventory()
    service, deploy, pod, endpoints = items
    if fault == "port":
        service["spec"]["ports"][0]["targetPort"] = 8081
        endpoints["ports"][0]["port"] = 8081
    elif fault == "selector":
        service["spec"]["selector"]["app"] = "app-v2"
        endpoints["endpoints"] = []
    elif fault == "replicas":
        deploy["spec"]["replicas"] = deploy["status"]["replicas"] = 0
        items.remove(pod)
        endpoints["endpoints"] = []
    elif fault == "readiness":
        pod["spec"]["containers"][0]["readinessProbe"] = {
            "httpGet": {"path": "/healthz", "port": 8081}
        }
        pod["status"]["conditions"][0]["status"] = "False"
        pod["status"]["containerStatuses"][0]["ready"] = False
        endpoints["endpoints"][0]["conditions"]["ready"] = False
        items.append(
            {
                "kind": "Event",
                "involvedObject": {"uid": "pod-uid"},
                "message": "Readiness probe failed: dial tcp 10.42.1.2:8081: connect: connection refused",
            }
        )
    other_deploy, other_pod = copy.deepcopy([deploy, pod])
    other_deploy["metadata"]["name"] = "database"
    other_deploy["spec"].update(
        replicas=1,
        selector={"matchLabels": {"app": "database"}},
        template={"metadata": {"labels": {"app": "database"}}},
    )
    other_pod["metadata"].update(
        name="database-1", uid="db-uid", labels={"app": "database"}
    )
    other_pod["status"].update(
        conditions=[{"type": "Ready", "status": "True"}],
        containerStatuses=[
            {"ready": True, "state": {"running": {"startedAt": "2026-11-02T00:00:00Z"}}}
        ],
    )
    items.extend([other_deploy, other_pod])
    output = json.dumps({"items": items})
    for cls, expected in [
        (ServiceTargetPortMismatch, "port"),
        (ServiceSelectorMismatch, "selector"),
        (DeploymentReplicasZero, "replicas"),
        (ReadinessProbeMisconfig, "readiness"),
    ]:
        assert _check(cls)(output) is (fault == expected)


def test_target_port_evidence_requires_ready_endpoints_and_a_working_application():
    items = _inventory()
    items[0]["spec"]["ports"][0]["targetPort"] = 8081
    items[3]["ports"][0]["port"] = 8081
    check = _check(ServiceTargetPortMismatch)
    assert check(json.dumps({"items": items}))
    absent = copy.deepcopy(items)
    absent[3]["endpoints"] = []
    assert not check(json.dumps({"items": absent}))
    assert not _check(ServiceTargetPortMismatch, service_name="another")(
        json.dumps({"items": items})
    )
    problem = ServiceTargetPortMismatch(None)
    probe = problem.oracle_playbook(problem.Params(control_node="controller")).steps[1]
    assert probe.expect("target NO_CONNECT\ndeclared CONNECTED\n")
    assert not probe.expect("target NO_CONNECT\ndeclared NO_CONNECT\n")
    assert not probe.expect("target CONNECTED\ndeclared CONNECTED\n")


def test_loadbalancer_requires_allocated_ips_and_no_advertisement_of_a_real_pool():
    items = [
        {"kind": "IPAddressPool", "metadata": {"name": "public"}},
        {"kind": "BGPAdvertisement", "spec": {"ipAddressPools": ["retired"]}},
        {
            "kind": "Service",
            "spec": {"type": "LoadBalancer"},
            "status": {"loadBalancer": {"ingress": [{"ip": "192.0.2.1"}]}},
        },
    ]
    check = _check(LoadBalancerAdvertisementMissing)
    assert check(json.dumps({"items": items}))
    items.append({"kind": "L2Advertisement", "spec": {"ipAddressPools": ["public"]}})
    assert not check(json.dumps({"items": items}))
    items.pop()
    items[-1]["status"] = {}
    assert not check(json.dumps({"items": items}))


def test_ingress_class_evidence_requires_undefined_class_and_ready_backend():
    items = [
        {
            "kind": "Ingress",
            "metadata": {"namespace": "business"},
            "spec": {
                "ingressClassName": "retired",
                "rules": [
                    {"http": {"paths": [{"backend": {"service": {"name": "app"}}}]}}
                ],
            },
        },
        {"kind": "IngressClass", "metadata": {"name": "nginx"}},
        _inventory()[-1],
    ]
    check = _check(IngressClassMismatch)
    assert check(json.dumps({"items": items}))
    items[0]["spec"]["ingressClassName"] = "nginx"
    assert not check(json.dumps({"items": items}))
    items[0]["spec"]["ingressClassName"] = "retired"
    items[-1]["endpoints"][0]["conditions"]["ready"] = False
    assert not check(json.dumps({"items": items}))


def test_target_port_can_point_at_a_live_health_listener():
    items = _inventory()
    service, _, pod, endpoints = items
    service["spec"]["ports"][0].update(name="grpc", targetPort=9003)
    pod["spec"]["containers"][0]["ports"] = [
        {"name": "grpc", "containerPort": 9002},
        {"name": "health", "containerPort": 9003},
    ]
    endpoints["ports"][0]["port"] = 9003
    output = json.dumps({"items": items})
    problem = ServiceTargetPortMismatch(None)
    plan = problem.oracle_playbook(problem.Params(control_node="controller"))
    assert plan.steps[0].expect(output)
    args = plan.steps[1].args({"port_inventory": output})
    assert "target_name=health service_port_name=grpc" in args["command"]
    assert plan.steps[1].expect(
        "target_name=health service_port_name=grpc\ntarget CONNECTED\ndeclared CONNECTED\n"
    )
    service["spec"]["ports"][0]["targetPort"] = 9002
    endpoints["ports"][0]["port"] = 9002
    assert not plan.steps[0].expect(json.dumps({"items": items}))


def test_noexecute_evidence_requires_ready_node_and_untolerated_taint():
    problem = NodeNoExecuteTaint(None)
    params = problem.Params(control_node="controller", node_name="worker1")
    check = problem.oracle_playbook(params).steps[0].expect
    items = [
        {
            "kind": "Node",
            "metadata": {"name": "worker1"},
            "spec": {
                "taints": [
                    {
                        "key": params.taint_key,
                        "value": params.taint_value,
                        "effect": "NoExecute",
                    }
                ]
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "kind": "Pod",
            "status": {
                "phase": "Pending",
                "conditions": [
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": f"node had untolerated taint {{{params.taint_key}: {params.taint_value}}}",
                    }
                ],
            },
        },
    ]
    assert check(json.dumps({"items": items}))
    items[0]["spec"]["taints"][0]["effect"] = "NoSchedule"
    assert not check(json.dumps({"items": items}))
    items[0]["spec"]["taints"][0]["effect"] = "NoExecute"
    items[0]["status"]["conditions"][0]["status"] = "Unknown"
    assert not check(json.dumps({"items": items}))


def test_selector_projection_preserves_null_endpoint_observation(monkeypatch, capsys):
    items = _inventory()
    items[0]["spec"]["selector"]["app"] = "app-v2"
    items[-1]["endpoints"] = None
    monkeypatch.setattr(
        subprocess, "check_output", lambda *_: json.dumps({"items": items}).encode()
    )
    problem = ServiceSelectorMismatch(None)
    params = problem.Params(
        control_node="controller", service_name="app", namespace="business"
    )
    step = problem.oracle_playbook(params).steps[0]
    script = shlex.split(step.args["command"])[2]
    exec(compile(script, "oracle_service_query", "exec"), {})
    output = capsys.readouterr().out
    assert len(output) < 10000
    assert step.expect(output)


def test_readiness_projection_excludes_healthy_system_probe_namespaces(
    monkeypatch, capsys
):
    _, deploy, pod, _ = _inventory()
    pod["spec"]["containers"][0]["readinessProbe"] = {"tcpSocket": {"port": 8081}}
    healthy = copy.deepcopy(pod)
    healthy["metadata"].update(namespace="monitoring", uid="healthy-uid")
    pod["status"]["containerStatuses"][0]["ready"] = False
    event = {
        "kind": "Event",
        "metadata": {"name": "probe-failure", "namespace": "business"},
        "involvedObject": {"uid": "pod-uid"},
        "message": "Readiness probe failed: dial tcp 10.42.1.2:8081: connection refused",
    }
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"items": [deploy, pod, healthy, event]}))
    )
    problem = ReadinessProbeMisconfig(None)
    step = problem.oracle_playbook(problem.Params(control_node="controller")).steps[0]
    script = shlex.split(step.args["command"])[-1]
    exec(compile(script, "oracle_workload_query", "exec"), {})
    output = capsys.readouterr().out
    assert {o["metadata"]["namespace"] for o in json.loads(output)["items"]} == {
        "business"
    }
    assert step.expect(output)
