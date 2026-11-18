"""NetFlow kind: pmacct on FRR, OVS NetFlow on SDN switches."""

from __future__ import annotations

import pytest

from nika.telemetry import netflow

pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs

    def exec(self, name: str, command: str, timeout: float = 10) -> str:
        return "\n".join(
            output for needle, output in self.outputs.items() if needle in command
        )


def test_probe_prefers_pmacct_over_ovs() -> None:
    runtime = _Runtime(
        {
            "pmacctd -V": "Promiscuous Mode Accounting Daemon, pmacctd 1.7.7-git",
            "ovs-vsctl --version": "ovs-vsctl (Open vSwitch) 3.1.0",
        }
    )
    assert netflow.probe(runtime, "r1") == "1.7.7-git"


def test_probe_falls_back_to_ovs() -> None:
    runtime = _Runtime({"ovs-vsctl --version": "ovs-vsctl (Open vSwitch) 3.1.0\n"})
    assert netflow.probe(runtime, "leaf_1") == "ovs-3.1.0"


def test_probe_returns_none_without_exporters() -> None:
    assert netflow.probe(_Runtime({}), "pc1") is None


def test_probe_falls_back_to_srl() -> None:
    runtime = _Runtime(
        {
            "command -v sr_cli": "/usr/bin/sr_cli",
            "show version": "Software Version: v24.10.1",
        }
    )
    assert netflow.probe(runtime, "leaf1") == "srl-24.10.1"
    assert (
        netflow.device_files({}, "172.26.0.1", "172.26.255.254", version="srl-24.10.1")
        == {}
    )
    assert netflow.device_lines({}, "172.26.0.1", version="srl-24.10.1") == []


def test_probe_falls_back_to_routeros() -> None:
    runtime = _Runtime(
        {
            "command -v sshpass": "/usr/bin/sshpass",
            "/system resource print": "version: 7.21.5\nuptime: 1m",
        }
    )
    assert netflow.probe(runtime, "router1") == "ros-7.21.5"
    assert (
        netflow.device_files({}, "172.28.0.1", "172.28.255.254", version="ros-7.21.5")
        == {}
    )
    assert netflow.device_lines({}, "172.28.0.1", version="ros-7.21.5") == []


def test_probe_skips_iosxr_for_netflow() -> None:
    runtime = _Runtime(
        {
            "test -x /pkg/bin/xr_cli": "xr_cli",
            "command -v python3": "/usr/bin/python3",
            "show version": "Cisco IOS XR Software, Version 26.2.1",
        }
    )
    assert netflow.probe(runtime, "router1") is None


def test_ovs_netflow_script_exports_to_collector() -> None:
    script = netflow.ovs_netflow_script("172.28.0.254", "172.28.0.3")
    assert "nika" not in script.lower()
    assert 'targets=\\"172.28.0.254:9995\\"' in script
    assert "create NetFlow" in script
    assert "active_timeout=10" in script
    assert "172.28.0.3" in script
    files = netflow.device_files(
        {"data_ifaces": ["eth0"]},
        "172.28.0.3",
        "172.28.0.254",
        version="ovs-3.1.0",
    )
    assert files == {netflow.NETFLOW_SCRIPT: script}
    assert netflow.device_lines({}, "172.28.0.3", version="ovs-3.1.0") == [
        f"sh {netflow.NETFLOW_SCRIPT}"
    ]


def test_running_accepts_ovs_netflow() -> None:
    runtime = _Runtime(
        {
            "pgrep -x nfcapd": "12",
            "pgrep -x pmacctd": "",
            "list NetFlow": 'targets : ["172.28.0.254:9995"]\n',
        }
    )
    assert netflow.running(runtime, "telemetry_collector", {"leaf_1": "172.28.0.3"})


def test_pmacct_path_unchanged_without_version() -> None:
    files = netflow.device_files(
        {"data_ifaces": ["eth0", "eth1"]}, "172.28.0.3", "172.28.0.254"
    )
    assert set(files) == {
        netflow.pmacct_conf_path("eth0"),
        netflow.pmacct_conf_path("eth1"),
    }
    lines = netflow.device_lines({"data_ifaces": ["eth0"]}, "172.28.0.3")
    assert "pmacctd" in lines[0]
