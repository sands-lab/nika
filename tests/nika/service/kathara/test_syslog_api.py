"""Contracts for the syslog ``syslog_query`` service API."""

from __future__ import annotations

import pytest

from nika.service.kathara.syslog_api import SyslogAPIMixin
from nika.telemetry.syslog import SYSLOG_DIR, SYSLOG_HOSTS

pytestmark = pytest.mark.unit


class _FakeCollector(SyslogAPIMixin):
    def __init__(self, hosts: str, logs: str) -> None:
        self.hosts = hosts
        self.logs = logs
        self.commands: list[str] = []

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        assert host_name == "telemetry_collector"
        self.commands.append(command)
        if SYSLOG_HOSTS in command:
            return self.hosts + "\n__rc=0"
        return self.logs + "\n__rc=0"


def test_syslog_query_filters_host_program_and_substring() -> None:
    api = _FakeCollector(
        "172.28.0.3 leaf_router_0_0\n",
        "\n".join(
            [
                "2026-10-26T07:00:01+00:00 leaf_router_0_0 bgpd: neighbor 10.0.0.2 Up",
                "2026-10-26T07:00:02+00:00 leaf_router_0_0 zebra: interface eth1 up",
                "2026-10-26T07:00:03+00:00 leaf_router_0_0 bgpd: telemetry_collector skip",
            ]
        ),
    )
    rows = api.syslog_query(
        start_time="0",
        host="leaf_router_0_0",
        program="bgpd",
        contains="neighbor",
        limit=10,
    )
    assert rows == [
        {
            "time": "2026-10-26T07:00:01+00:00",
            "host": "leaf_router_0_0",
            "program": "bgpd",
            "message": "neighbor 10.0.0.2 Up",
        }
    ]
    assert SYSLOG_DIR in api.commands[-1]
    assert "172.28.0.3.log" in api.commands[-1]


def test_syslog_query_maps_fromhost_ip_to_device_name() -> None:
    api = _FakeCollector(
        "172.28.0.3 leaf_router_0_0\n",
        "2026-10-26T07:00:01+00:00 172.28.0.3 bgpd: neighbor 10.0.0.2 Up\n",
    )
    rows = api.syslog_query(start_time="0", host="leaf_router_0_0", limit=10)
    assert rows == [
        {
            "time": "2026-10-26T07:00:01+00:00",
            "host": "leaf_router_0_0",
            "program": "bgpd",
            "message": "neighbor 10.0.0.2 Up",
        }
    ]


def test_syslog_query_rejects_unknown_host() -> None:
    api = _FakeCollector("172.28.0.3 leaf_router_0_0\n", "")
    with pytest.raises(ValueError, match="Unknown host"):
        api.syslog_query(start_time="-60", host="spine")


@pytest.mark.parametrize(
    "raw", ["tail: cannot open log\n__rc=1", "Error executing command"]
)
def test_syslog_query_reports_failed_observation(raw) -> None:
    api = _FakeCollector("172.28.0.3 leaf_router_0_0\n", "")
    execute = api.exec_cmd
    api.exec_cmd = lambda host, command, timeout=10: (
        execute(host, command, timeout) if SYSLOG_HOSTS in command else raw
    )
    with pytest.raises(RuntimeError, match="Observation"):
        api.syslog_query(start_time="0")


def test_syslog_empty_log_is_healthy() -> None:
    api = _FakeCollector("172.28.0.3 leaf_router_0_0\n", "")
    assert api.syslog_query(start_time="0") == []


@pytest.mark.parametrize("host", [None, "leaf"])
def test_syslog_missing_host_log_and_empty_directory(tmp_path, monkeypatch, host):
    import subprocess

    import nika.service.kathara.syslog_api as module

    monkeypatch.setattr(module, "SYSLOG_DIR", str(tmp_path))
    api = _FakeCollector("192.0.2.1 leaf\n", "")
    original = api.exec_cmd

    def execute(node, command, timeout=10):
        if SYSLOG_HOSTS in command:
            return original(node, command, timeout)
        return subprocess.run(
            command, shell=True, capture_output=True, text=True
        ).stdout

    api.exec_cmd = execute
    assert api.syslog_query(start_time="0", host=host) == []
    (tmp_path / "192.0.2.1.log").write_text(
        "2026-10-26T07:00:01+00:00 192.0.2.1 bgpd: ready\n"
    )
    assert api.syslog_query(start_time="0", host=host)[0]["message"] == "ready"
    monkeypatch.setattr(module, "SYSLOG_DIR", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="Observation"):
        api.syslog_query(start_time="0", host=host)
