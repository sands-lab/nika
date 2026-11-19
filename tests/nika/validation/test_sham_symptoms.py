"""Healthy probes run before injection captures recovery state."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from nika.problems.service_networking.kubernetes import IngressClassMismatch
from nika.validation.symptom import k8s_probes, routing_probes

pytestmark = pytest.mark.unit


def test_ingress_symptom_before_injection_and_after_route_loss(monkeypatch):
    ingress = {
        "metadata": {"namespace": "app", "name": "ingress"},
        "spec": {
            "rules": [
                {
                    "host": "example.test",
                    "http": {
                        "paths": [
                            {"path": "/app", "backend": {"service": {"name": "app"}}}
                        ]
                    },
                }
            ]
        },
    }
    problem = IngressClassMismatch()
    problem.control_node = lambda params: "controller"
    problem.runtime = SimpleNamespace(
        lab_api=SimpleNamespace(kubectl_json=lambda *args: {"items": [ingress]})
    )
    sample = {
        "codes": {
            "http://example.test/app": "200",
            "http://example.test/control": "200",
        },
        "backends": {"app/app": {"endpoints": ["10.42.0.1"], "reachable": True}},
    }
    problem._audit_ingress_class = deepcopy(sample)
    monkeypatch.setattr(k8s_probes, "_ingress_class_sample", lambda p: sample)
    monkeypatch.setattr(k8s_probes, "_wait_for", lambda check: check())
    assert not hasattr(problem, "_original")
    observed, evidence = k8s_probes.ingress_class_broken(problem, object())
    assert observed is False
    assert evidence["details"]["targets"] == ["http://example.test/app"]
    problem._original = ingress
    sample["codes"]["http://example.test/app"] = "404"
    assert k8s_probes.ingress_class_broken(problem, object())[0] is True
    sample["codes"]["http://example.test/control"] = "404"
    assert k8s_probes.ingress_class_broken(problem, object())[0] is False


def test_timer_symptom_before_injection_and_after_adjacency_loss(monkeypatch):
    health = {"router": {"full": True, "interface_up": True, "routes": ["10.0.0.0/24"]}}
    problem = SimpleNamespace(
        root_cause_name="ospf_hello_interval_mismatch",
        runtime=object(),
        _audit_ospf_cut_link={
            "host": "router",
            "intf": "eth0",
            "src": "client",
            "dst": "10.0.0.1",
            "destination_routes": {"router": ["10.0.0.0/24"]},
        },
        _timers=lambda *args: (10, 40),
        link_health=lambda p: health,
    )
    params = SimpleNamespace(host_name="router", intf_name="eth0")
    clock = iter([0, 101, 200])
    monkeypatch.setattr(routing_probes.time, "monotonic", lambda: next(clock))
    loss = SimpleNamespace(loss_percent=0.0)
    monkeypatch.setattr(routing_probes, "ping_stats", lambda *a, **kw: loss)
    assert routing_probes.ospf_timer_mismatch(problem, params)[0] is False
    problem._original_timers, problem.injected_timers = (10, 40), (5, 20)
    health["router"].update(full=False, routes=[])
    loss.loss_percent = 100.0
    assert routing_probes.ospf_timer_mismatch(problem, params)[0] is True


@pytest.mark.parametrize("profile", [None, "iosxr"])
@pytest.mark.parametrize("misleading_router_name", [False, True])
def test_icmp_probe_uses_declared_endpoint_roles(
    monkeypatch, profile, misleading_router_name
):
    from types import SimpleNamespace

    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.runtime.spec import NodeIdentity, NodeRole
    from nika.validation.symptom import icmp_probes

    env = get_net_env_instance(
        "enterprise_branch", topo_size="s", device_profile=profile
    )
    if misleading_router_name:
        env.machine_identities["client_router"] = NodeIdentity(role=NodeRole.ROUTER)
    runtime = SimpleNamespace(
        get_connected_devices=lambda node: {"br1_corp_pc", "br1_guest_pc"},
        list_nodes=lambda: list(env.machine_identities),
    )
    problem = SimpleNamespace(net_env=env, runtime=runtime)
    addresses = {"br1_corp_pc": "10.1.10.2", "br2_corp_pc": "10.2.10.2"}
    monkeypatch.setattr(
        icmp_probes, "_data_ipv4", lambda problem, node: addresses[node]
    )
    queried = []

    def ping(runtime, sender, receiver, **kwargs):
        assert env.machine_identities[sender].role == NodeRole.HOST
        queried.append(sender)
        return SimpleNamespace(loss_percent=0.0)

    monkeypatch.setattr(icmp_probes, "ping_stats", ping)
    ends = icmp_probes._endpoints(problem, "br1_edge")
    assert ends["sender"] == "br2_corp_pc"
    assert ends["receiver"] == "br1_corp_pc"
    assert queried == ["br2_corp_pc"]
