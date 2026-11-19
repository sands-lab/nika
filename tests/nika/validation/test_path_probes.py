"""Path probes count replies from both iputils and BusyBox hosts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika.validation.symptom.path_probes import echo
from nika.validation.symptom.probe import run_probe_snapshot
from nika.net_env.base import ProbePath

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("interface", "expected"),
    [
        ("eth0", {"src": "remote", "dst": "10.0.1.2", "probe": "forward_df"}),
        ("eth1", {"src": "client", "dst": "192.0.2.1", "probe": "forward_df"}),
    ],
)
def test_native_mtu_probes_use_real_router_addresses_and_host_traffic(
    monkeypatch, interface, expected
):
    from nika.problems.support import router_ops
    from nika.runtime.spec import NodeRole
    from nika.validation.symptom import path_probes

    env = SimpleNamespace(
        machine_identities={
            "router": SimpleNamespace(role=NodeRole.ROUTER),
            "peer": SimpleNamespace(role=NodeRole.ROUTER),
            "client": SimpleNamespace(role=NodeRole.HOST),
            "remote": SimpleNamespace(role=NodeRole.HOST),
        },
        get_lab_spec=lambda: SimpleNamespace(
            links=[
                SimpleNamespace(endpoints=("router:eth0", "client:eth0")),
                SimpleNamespace(endpoints=("router:eth1", "peer:eth0")),
                SimpleNamespace(endpoints=("peer:eth1", "remote:eth0")),
            ]
        ),
    )
    addresses = {
        ("router", "eth0"): [],  # This access port is an unaddressed bridge member.
        ("router", "eth1"): ["192.0.2.0/31"],
        ("peer", "eth0"): ["2001:db8::1/64", "192.0.2.1/31"],
    }
    monkeypatch.setattr(
        router_ops,
        "router_fault_ops",
        lambda _, node: (
            SimpleNamespace(
                profile="routeros",
                supports=lambda operation: operation == "interface_addresses",
                interface_addresses=lambda intf: addresses[node, intf],
            )
            if node in {"router", "peer"}
            else None
        ),
    )

    def kernel_address(_runtime, node, intf):
        assert (node, intf) == ("client", "eth0")
        return "10.0.1.2"

    monkeypatch.setattr(path_probes, "echo", lambda *args, **kwargs: {"received": 1})
    kernel_read = Mock(side_effect=kernel_address)
    monkeypatch.setattr(path_probes, "iface_ipv4", kernel_read)
    problem = SimpleNamespace(net_env=env, runtime=object())
    params = SimpleNamespace(host_name="router", intf_name=interface)
    assert path_probes._mtu_ends(problem, params) == expected
    assert kernel_read.call_count == (1 if interface == "eth0" else 0)


def test_native_mtu_probe_selects_reachable_attached_host(monkeypatch):
    from nika.problems.support import benchmark_targets, router_ops
    from nika.validation.symptom import path_probes

    env = SimpleNamespace(
        machine_identities={},
        get_lab_spec=lambda: SimpleNamespace(
            links=[SimpleNamespace(endpoints=("router:eth1", "peer:eth0"))]
        ),
    )
    monkeypatch.setattr(
        router_ops,
        "router_fault_ops",
        lambda *_: SimpleNamespace(
            profile="routeros",
            supports=lambda operation: operation == "interface_addresses",
            interface_addresses=lambda _: ["192.0.2.1/31"],
        ),
    )
    monkeypatch.setattr(
        benchmark_targets, "attached_end_hosts", lambda *_: ["isolated", "reachable"]
    )
    monkeypatch.setattr(
        path_probes,
        "echo",
        lambda _runtime, src, _dst, **_kwargs: {"received": int(src == "reachable")},
    )
    problem = SimpleNamespace(net_env=env, runtime=object())
    params = SimpleNamespace(host_name="router", intf_name="eth1")
    assert path_probes._mtu_ends(problem, params) == {
        "src": "reachable",
        "dst": "192.0.2.1",
        "probe": "forward_df",
    }


@pytest.mark.parametrize("sequence", ["icmp_seq", "seq"])
def test_echo_counts_unique_replies_from_linux_and_k3s(sequence):
    output = "\n".join(
        f"64 bytes from 192.0.2.1: {sequence}={seq} ttl=63 time=1.2 ms"
        for seq in (0, 1, 1)
    )
    runtime = SimpleNamespace(exec=Mock(return_value=output))
    sample = echo(runtime, "sender", "192.0.2.1", count=3)
    assert sample["received"] == 2
    assert sample["loss_percent"] == 33.3
    assert sample["ttls"] == [63]


@pytest.mark.parametrize(("exit_code", "healthy"), [(0, True), (28, False)])
def test_http_status_and_timing_describe_one_completed_request(exit_code, healthy):
    runtime = SimpleNamespace(exec=Mock(return_value=f"200 0.25 100 {exit_code}"))
    snapshot = run_probe_snapshot(
        runtime, "path_http", ProbePath(src_host="client", http_url="http://service/")
    )
    assert snapshot.http_ok is healthy
    assert snapshot.http_time_ms == (250.0 if healthy else None)
    assert runtime.exec.call_count == 1
