import re

import pytest

from nika.validation.symptom import corruption_probes as probes


@pytest.mark.unit
def test_capacity_counter_excludes_unrelated_receiver_bytes_and_cleans_up(monkeypatch):
    commands = []
    tag = None
    reads = iter((100, 1100))

    def checked(execute, host, command, *, timeout):
        nonlocal tag
        commands.append(command)
        if " -I INPUT " in command:
            tag = re.search(r"--comment ([0-9a-f]+)", command).group(1)
        if " -L INPUT " in command:
            return f"100 999999 ACCEPT all -- * * 0.0.0.0/0 0.0.0.0/0\n 2 {next(reads)} tcp -- * * 0.0.0.0/0 10.2.10.2 tcp dpt:15201 /* {tag} */"
        return ""

    monkeypatch.setattr(probes, "checked_exec", checked)
    monkeypatch.setattr(probes, "_start_iperf3_server", lambda *args: 123)
    stopped = []
    monkeypatch.setattr(
        probes, "_stop_iperf3_server", lambda *args: stopped.append(args)
    )
    monkeypatch.setattr(probes, "exec_or_empty", lambda *args, **kwargs: "")
    times = iter((10.0, 15.0))
    monkeypatch.setattr(probes.time, "monotonic", lambda: next(times))
    result = probes._delivered_rate(
        type("Runtime", (), {"exec": lambda *args: ""})(),
        {"src": "source", "dst": "10.2.10.2", "dst_host": "receiver"},
    )
    assert result["received_bytes"] == 1000
    assert result["received_bps"] == 1600
    assert stopped and any(" -D INPUT " in command for command in commands)
    assert all(" -j " not in command for command in commands if " INPUT " in command)


@pytest.mark.unit
def test_capacity_counter_is_removed_when_observation_fails(monkeypatch):
    commands = []

    def checked(execute, host, command, *, timeout):
        commands.append(command)
        return ""

    monkeypatch.setattr(probes, "checked_exec", checked)
    monkeypatch.setattr(probes, "_start_iperf3_server", lambda *args: 123)
    monkeypatch.setattr(probes, "_stop_iperf3_server", lambda *args: None)
    with pytest.raises(RuntimeError, match="counter is unavailable"):
        probes._delivered_rate(
            type("Runtime", (), {"exec": lambda *args: ""})(),
            {"src": "source", "dst": "10.2.10.2", "dst_host": "receiver"},
        )
    assert any(" -D INPUT " in command for command in commands)
