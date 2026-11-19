"""A missing first LB reading is distinct from a broken control connection."""

from types import SimpleNamespace
import http.client
import json
import sys
import threading
from contextlib import nullcontext

import pytest

from nika.traffic.workload import BusinessWorkload, WorkloadProcess
from nika.net_env.p4_dc_gateway import l4_lb
from nika.validation.symptom import p4_gateway_probes as probes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("broken", [False, True])
def test_baseline_waits_for_first_reading_and_still_requires_every_control(
    monkeypatch, broken
):
    client = SimpleNamespace(name="client_1", ip="192.0.2.11")
    problem = SimpleNamespace(
        runtime=object(),
        net_env=SimpleNamespace(
            business_workload=lambda: BusinessWorkload(
                (
                    WorkloadProcess(
                        "client_1", "browser", "vip-session", ("--pause", "8-24")
                    ),
                )
            ),
            model=SimpleNamespace(
                client_on_gateway=lambda _: client,
                switch_info={"gateway_1": SimpleNamespace(device_id=1)},
            ),
        ),
    )
    flows = {port: 1 for port in range(16)}
    if broken:
        flows.pop(0)
    reads = 0

    def read(*_):
        nonlocal reads
        reads += 1
        return {} if reads == 1 else flows

    monkeypatch.setattr(probes, "_lb_business_flows", read)
    monkeypatch.setattr(
        probes, "_lb_backends", lambda *_: {port: str(port % 2) for port in flows}
    )
    monkeypatch.setattr(probes.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        probes, "wait_until", lambda check, *a, **kw: check() or check()
    )
    monkeypatch.setattr(
        l4_lb, "learner_status", lambda *_: {"learned": 16, "insert_failures": 0}
    )
    ok, evidence = probes.lb_race_baseline(
        problem, SimpleNamespace(host_name="gateway_1")
    )
    assert reads >= 2
    assert evidence["initial_reading_ready"] is (not broken)
    assert evidence["alive"] == (15 if broken else 16)
    assert ok is (not broken)


@pytest.mark.parametrize("case", ["setup_retry", "exhausted", "slow_request"])
def test_connection_probe_preserves_complete_batches_through_setup_and_requests(
    monkeypatch, tmp_path, case
):
    connections = []
    progress = threading.Event()

    class Connection:
        def __init__(self, *args, **kwargs):
            self.index = len(connections) + 1
            self.closed = False
            self.sock = SimpleNamespace(
                getsockname=lambda: ("", 40000 + self.index),
                settimeout=lambda _: None,
            )
            connections.append(self)

        def connect(self):
            if case == "exhausted" or (case == "setup_retry" and self.index == 2):
                raise TimeoutError("temporary TCP setup failure")
            if case == "setup_retry" and self.index == 3:
                assert all(conn.closed for conn in connections[:2])

        def close(self):
            self.closed = True
            self.sock = None

        def request(self, *args):
            if case == "slow_request":
                if self.index == 1:
                    assert progress.wait(2), "another connection must keep progressing"
                else:
                    progress.set()

        def getresponse(self):
            return SimpleNamespace(read=lambda: b"ok")

    monkeypatch.setattr(http.client, "HTTPConnection", Connection)
    status = tmp_path / "flows.json"
    monkeypatch.setattr(
        sys, "argv", ["probe", "unused", "2", str(status), "0.001", "0.001"]
    )
    if case == "exhausted":
        with pytest.raises(TimeoutError):
            exec(probes._LB_KEEPALIVE, {})
        assert len(connections) == 3
        assert all(conn.closed for conn in connections)
        assert not status.exists()
    else:
        exec(probes._LB_KEEPALIVE, {})
        assert all(conn.closed for conn in connections)
        ports = (40003, 40004) if case == "setup_retry" else (40001, 40002)
        assert json.loads(status.read_text())["flows"] == {
            str(port): "alive" for port in ports
        }


def test_trial_waits_for_the_first_reading_after_setup_retries(monkeypatch):
    elapsed = 0.0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(probes.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(probes.time, "sleep", sleep)
    flows = {1: "alive", 2: "alive"}
    monkeypatch.setattr(
        probes, "_lb_flows", lambda *_: {"flows": flows} if elapsed >= 6 else {}
    )
    problem = SimpleNamespace(runtime=SimpleNamespace(exec=lambda *a, **kw: ""))
    assert probes._lb_start(problem, "client", 2, "/tmp/flows", 0.2, 15) == {
        "flows": flows
    }


def test_trial_rejects_connections_broken_before_pool_update(monkeypatch):
    from nika.net_env.p4_dc_gateway import l4_lb

    flows = {port: "alive" for port in range(8)}
    flows[0] = "broken"
    monkeypatch.setattr(probes, "_lb_start", lambda *_: {"opened": 0, "flows": flows})
    monkeypatch.setattr(probes, "_lb_backends", lambda *_: {})
    monkeypatch.setattr(probes, "paused_workload", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(probes, "endpoint_names", lambda _: [])
    monkeypatch.setattr(
        l4_lb, "routine_pool_update", lambda *_: pytest.fail("pre-broken trial")
    )
    problem = SimpleNamespace(
        runtime=SimpleNamespace(exec=lambda *a, **kw: ""),
        net_env=SimpleNamespace(business_workload=lambda: BusinessWorkload(())),
        _audit_lb={
            "client": "client",
            "client_ip": "192.0.2.11",
            "gateway": "gateway_1",
            "device_id": 1,
        },
    )
    ok, evidence = probes.lb_race(problem, SimpleNamespace())
    assert not ok
    assert evidence["error"] == "trial_connections_not_alive"


@pytest.mark.parametrize(
    ("control", "broken", "expected"),
    [
        ("original", 0, False),
        ("original", 5, False),
        ("original", 6, True),
        ("original", 8, True),
        ("replacement", 8, False),
        ("stalled", 8, False),
    ],
)
def test_trial_requires_original_control_sockets_and_response_progress(
    monkeypatch, control, broken, expected
):
    owners = {port: str(port % 2) for port in range(16)}
    initial = {port: 100 for port in owners}
    after = {port: 200 for port in owners}
    if control == "replacement":
        after[16] = after.pop(0)
    elif control == "stalled":
        after[0] = 100
    readings = iter([initial])
    monkeypatch.setattr(probes, "_lb_business_flows", lambda *_: next(readings, after))
    monkeypatch.setattr(probes, "_lb_backends", lambda *_: owners)
    monkeypatch.setattr(
        probes,
        "_lb_flows",
        lambda *_: {
            "flows": {port: "broken" if port < broken else "alive" for port in range(8)}
        },
    )
    monkeypatch.setattr(probes.time, "sleep", lambda _: None)
    monkeypatch.setattr(probes, "wait_until", lambda check, *a, **kw: check())
    monkeypatch.setattr(
        l4_lb, "routine_pool_update", lambda *_: {"ok": True, "time": 2}
    )
    state = {
        "client": "client",
        "client_ip": "192.0.2.1",
        "gateway": "gateway_1",
        "device_id": 1,
        "owners": owners,
        "control_wait_sec": 34,
    }
    problem = SimpleNamespace(
        runtime=None, root_cause_name="lb_pending_connection_update_race"
    )
    ok, evidence = probes._lb_race_trial(
        problem,
        state,
        "/tmp/trial",
        {"opened": 0, "flows": {port: "alive" for port in range(8)}},
    )
    assert ok is expected
    assert evidence["details"]["control_ok"] is (control == "original")


def test_trial_cleans_process_and_files_on_measurement_error(monkeypatch):
    calls = []
    problem = SimpleNamespace(
        runtime=SimpleNamespace(exec=lambda *a, **kw: calls.append(a[1]))
    )
    monkeypatch.setattr(probes, "_lb_start", lambda *_: {"opened": 1})
    with (
        pytest.raises(RuntimeError),
        probes._lb_trial(problem, "client", "/tmp/owned-trial"),
    ):
        raise RuntimeError("measurement failed")
    assert len(calls) == 1
    assert "kill" in calls[0] and "rm -f /tmp/owned-trial" in calls[0]


def test_business_control_inventory_excludes_other_browsers(monkeypatch, capsys):
    import subprocess

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="123\n")
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *a, **kw: (
            '0 0 192.0.2.11:40000 20.0.0.1:80 users:(("python3",pid=123,fd=3)) bytes_received:197\n'
            '0 0 192.0.2.11:40001 20.0.0.1:80 users:(("python3",pid=124,fd=3)) bytes_received:200\n'
        ),
    )
    monkeypatch.setattr(sys, "argv", ["inspect", "20.0.0.1"])
    exec(probes._LB_BUSINESS_READ, {})
    assert json.loads(capsys.readouterr().out) == {"40000": 197}
