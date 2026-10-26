"""Syslog kind: parsers, Linux forwarder, and vendor configs."""

from __future__ import annotations

import pytest

from nika.telemetry import routeros, srl, syslog

pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs

    def exec(self, name: str, command: str, timeout: float = 10) -> str:
        return "\n".join(
            output for needle, output in self.outputs.items() if needle in command
        )


def test_parse_log_line_splits_fields() -> None:
    row = syslog.parse_log_line(
        "2026-10-26T07:00:00+00:00 leaf_router_0_0 bgpd[12]: neighbor 10.0.0.2 Up"
    )
    assert row == {
        "time": "2026-10-26T07:00:00+00:00",
        "host": "leaf_router_0_0",
        "program": "bgpd",
        "message": "neighbor 10.0.0.2 Up",
    }


def test_parse_log_line_drops_collector_and_benchmark_names() -> None:
    assert (
        syslog.parse_log_line(
            "2026-10-26T07:00:00+00:00 leaf rsyslogd: telemetry_collector started"
        )
        is None
    )
    assert (
        syslog.parse_log_line("2026-10-26T07:00:00+00:00 leaf zebra: nika session id")
        is None
    )


def test_probe_unix_when_python3_exists() -> None:
    runtime = _Runtime({"command -v python3": "/usr/bin/python3"})
    assert syslog.probe(runtime, "leaf_router_0_0") == syslog.UNIX_VERSION


def test_device_files_bake_collector_into_linux_forwarder() -> None:
    files = syslog.device_files(
        {"mgmt_iface": "eth2"},
        "172.28.0.3",
        "172.28.0.254",
        version=syslog.UNIX_VERSION,
    )
    script = files[syslog.SYSLOG_FORWARDER]
    assert "nika" not in script.lower()
    assert "172.28.0.254" in script
    assert "SO_BINDTODEVICE" in script
    lines = syslog.device_lines(
        {"mgmt_iface": "eth2"}, "172.28.0.3", version=syslog.UNIX_VERSION
    )
    assert syslog.SYSLOG_FORWARDER in "\n".join(lines)
    assert "log syslog" in "\n".join(lines)


def test_collector_rsyslog_keys_files_by_source_ip() -> None:
    files = syslog.collector_files({"leaf": "172.28.0.3"})
    assert "%fromhost-ip%" in files[syslog.SYSLOG_CONFIG]
    assert "172.28.0.3 leaf" in files[syslog.SYSLOG_HOSTS]


def test_xr_syslog_uses_linux_forwarder() -> None:
    files = syslog.device_files(
        {"mgmt_iface": "eth2"},
        "172.28.0.3",
        "172.28.0.254",
        version="xr-26.2.1",
    )
    assert "SO_BINDTODEVICE" in files[syslog.SYSLOG_FORWARDER]
    lines = syslog.device_lines(
        {"mgmt_iface": "eth2"}, "172.28.0.3", version="xr-26.2.1"
    )
    assert syslog.SYSLOG_FORWARDER in "\n".join(lines)


def test_routeros_syslog_script_adds_remote_action() -> None:
    script = routeros.syslog_script("172.28.0.1", "172.28.0.254")
    assert "action set remote" in script
    assert "remote=172.28.0.254" in script
    assert "src-address=172.28.0.1" in script
    assert "{" not in script
    assert "nika" not in script.lower()


def test_srl_syslog_enabled_reads_running_logging() -> None:
    runtime = _Runtime(
        {
            "info from running system logging": (
                "    logging {\n"
                "        remote-server 172.100.8.10 {\n"
                "            transport udp\n"
                "            remote-port 514\n"
                "        }\n"
                "    }\n"
            )
        }
    )
    assert srl.syslog_enabled(runtime, "leaf1")


def test_srl_syslog_uses_remote_server_leaves(monkeypatch) -> None:
    seen: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(srl, "gnmic_set", lambda ip, updates: seen.append(updates))
    srl.enable_syslog(mgmt_ip="172.100.8.2", collector="172.100.8.10")
    paths = [path for path, _ in seen[0]]
    assert ("/system/logging/network-instance", "mgmt") in seen[0]
    assert all(not path.endswith("source-address") for path in paths)
    assert all(
        "network-instance" not in path or path == "/system/logging/network-instance"
        for path in paths
    )
    assert any(path.endswith("/transport") for path in paths)
    assert any(path.endswith("/remote-port") for path in paths)
    assert "informational" in {value for _, value in seen[0]}
    assert any("facility-name=local6" in path for path in paths)
