from types import SimpleNamespace

import pytest

from nika.validation.symptom import addressing_probes as probes


@pytest.mark.unit
def test_netmask_control_uses_local_gateway_when_no_sibling_is_visible(monkeypatch):
    class Runtime:
        def get_host_ip(self, host, intf, *, with_prefix):
            return "10.1.10.2/24"

        def get_default_gateway(self, host):
            return "10.1.10.1"

    problem = SimpleNamespace(
        runtime=Runtime(),
        net_env=SimpleNamespace(machine_identities={}),
        root_cause_name="host_incorrect_netmask",
    )
    params = SimpleNamespace(host_name="target", netmask_prefix=8)
    monkeypatch.setattr(
        probes, "lab_addresses", lambda runtime: {"remote": [("eth0", "10.2.10.2/24")]}
    )
    monkeypatch.setattr(
        probes,
        "_reachable",
        lambda runtime, source, ip: (
            (source, ip) in {("remote", "10.1.10.2"), ("target", "10.1.10.1")}
        ),
    )
    monkeypatch.setattr(probes, "_healthy_loss", lambda runtime, source, ip: 0.0)
    healthy, baseline = probes.netmask_baseline(problem, params)
    assert healthy
    assert (baseline["control_observer"], baseline["control_ip"]) == (
        "target",
        "10.1.10.1",
    )
    losses = {("remote", "10.1.10.2"): 100.0, ("target", "10.1.10.1"): 0.0}
    monkeypatch.setattr(
        probes, "_loss", lambda runtime, source, ip: losses[(source, ip)]
    )
    assert probes.netmask_offsubnet_unreachable(problem, params)[0]
    losses[("target", "10.1.10.1")] = 100.0
    assert not probes.netmask_offsubnet_unreachable(problem, params)[0]


@pytest.mark.unit
def test_netmask_control_rejects_a_gateway_outside_the_original_subnet(monkeypatch):
    class Runtime:
        def get_host_ip(self, host, intf, *, with_prefix):
            return "10.1.10.2/24"

        def get_default_gateway(self, host):
            return "10.2.10.1"

    problem = SimpleNamespace(
        runtime=Runtime(), net_env=SimpleNamespace(machine_identities={})
    )
    params = SimpleNamespace(host_name="target", netmask_prefix=8)
    monkeypatch.setattr(
        probes, "lab_addresses", lambda runtime: {"remote": [("eth0", "10.2.10.2/24")]}
    )
    monkeypatch.setattr(probes, "_reachable", lambda runtime, source, ip: True)
    ok, evidence = probes.netmask_baseline(problem, params)
    assert not ok
    assert evidence["error"] == "no_sibling_in_the_real_subnet"
