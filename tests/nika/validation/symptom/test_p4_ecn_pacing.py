"""ECN probes preserve pacing after a sender scheduling delay."""

import builtins
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from nika.validation.symptom.p4_gateway_probes import _ECN_BURST
from nika.validation.symptom import p4_gateway_probes as probes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("eventually_marks", [False, True])
def test_ecn_baseline_waits_for_marks_without_resuming_traffic(
    monkeypatch, eventually_marks
):
    transitions = []

    @contextmanager
    def pause(*_args, **_kwargs):
        transitions.append("paused")
        yield
        transitions.append("resumed")

    calls = 0

    def burst(*_args):
        nonlocal calls
        calls += 1
        return {"total": 900 + calls, "ce": 20 if eventually_marks and calls > 1 else 0}

    problem = SimpleNamespace(
        runtime=object(),
        net_env=SimpleNamespace(business_workload=lambda: None),
    )
    monkeypatch.setattr(probes, "paused_workload", pause)
    monkeypatch.setattr(probes, "endpoint_names", lambda _: [])
    monkeypatch.setattr(probes, "_ecn_burst_counted", burst)
    monkeypatch.setattr(
        probes, "_ecn_ports", lambda *_: {"probed": object(), "sibling": object()}
    )
    monkeypatch.setattr(
        probes, "_ecn_flows", lambda *_: {"client": ["10.0.2.11:10007"]}
    )
    ok, evidence = probes.ecn_marking_baseline(
        problem, SimpleNamespace(host_name="spine_1", bmv2_port=10)
    )
    assert ok is eventually_marks
    assert evidence["probed"]["readings"][0]["ce"] == 0
    assert len(evidence["probed"]["readings"]) == calls
    assert calls == (2 if eventually_marks else probes._ECN_BASELINE_ATTEMPTS)
    assert transitions == ["paused", "resumed"]


def test_path_discovery_rejects_late_report_from_another_client(monkeypatch):
    own = {"src": "192.0.2.12", "src_port": 10007, "egress_port": 2}
    late = {"src": "192.0.2.11", "src_port": 10007, "egress_port": 3}
    problem = SimpleNamespace(
        runtime=SimpleNamespace(exec=lambda *_args, **_kwargs: "")
    )
    monkeypatch.setattr(probes, "_clock_ns", lambda _: 123)
    monkeypatch.setattr(probes.time, "sleep", lambda _: None)
    monkeypatch.setattr(probes, "_reports_since", lambda *_: [own, late])
    assert probes._send(
        problem,
        {"client": "client_2", "client_ip": "192.0.2.12", "service_ip": "10.0.2.11"},
        200,
        [10007],
    ) == [own]


def test_sender_does_not_catch_up_with_unpaced_datagrams():
    now = 0.0
    sent = []

    def sleep(seconds):
        nonlocal now
        now += seconds

    def sendto(*_args):
        nonlocal now
        sent.append(now)
        if len(sent) == 2:
            now += 0.25  # A descheduled sender misses several packet intervals.

    sock = SimpleNamespace(
        setsockopt=lambda *_: None, bind=lambda *_: None, sendto=sendto
    )
    modules = {
        "socket": SimpleNamespace(
            socket=lambda *_: sock,
            AF_INET=2,
            SOCK_DGRAM=2,
            SOL_SOCKET=1,
            SO_REUSEADDR=2,
            IPPROTO_IP=0,
            IP_TOS=1,
        ),
        "time": SimpleNamespace(time=lambda: now, sleep=sleep),
        "sys": SimpleNamespace(
            argv=["sender", "10.0.0.1:10007", "19600", "100", "1", "0"]
        ),
    }
    namespace = {
        "__builtins__": {
            **vars(builtins),
            "__import__": lambda name, *_args: modules[name],
        }
    }
    exec(_ECN_BURST, namespace)
    assert len(sent) > 2
    assert all(after - before >= 0.01 - 1e-9 for before, after in zip(sent, sent[1:]))


@pytest.mark.parametrize("fails", [False, True])
def test_burst_removes_receiver_and_result_even_when_sending_fails(monkeypatch, fails):
    calls = []

    def execute(node, command, **kwargs):
        calls.append((node, command))
        if node == "client" and fails:
            raise RuntimeError("send failed")
        return '{"total": 100, "ce": 7}' if command.startswith("cat ") else ""

    problem = SimpleNamespace(
        runtime=SimpleNamespace(exec=execute),
        net_env=SimpleNamespace(
            model=SimpleNamespace(
                services=[SimpleNamespace(name="service", ip="192.0.2.1")]
            )
        ),
    )
    monkeypatch.setattr(probes.time, "sleep", lambda _: None)
    monkeypatch.setattr(probes.time, "time_ns", lambda: 17)
    if fails:
        with pytest.raises(RuntimeError, match="send failed"):
            probes._ecn_burst_counted(problem, {"client": ["192.0.2.1:10007"]})
    else:
        assert probes._ecn_burst_counted(problem, {"client": ["192.0.2.1:10007"]}) == {
            "total": 100,
            "ce": 7,
        }
    node, cleanup = calls[-1]
    assert node == "service" and "kill" in cleanup
    assert "rm -f /tmp/probe-ecn-17.json" in cleanup
