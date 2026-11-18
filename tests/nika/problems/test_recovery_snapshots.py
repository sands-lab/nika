"""Recovery preserves file contents and rejects shell failures."""

import subprocess
from types import SimpleNamespace

import pytest

from nika.problems.support.recovery import (
    DHCPRecovery,
    FileSnapshot,
    IPv4Snapshot,
    checked_exec,
    route_lines,
)

pytestmark = pytest.mark.unit


class _ShellRuntime:
    def exec(self, node, command):
        return subprocess.run(
            command, shell=True, capture_output=True, text=True, check=False
        ).stdout


@pytest.mark.parametrize(
    "content", [None, "", "$(touch forbidden) `false` 'quote' π\nsecond\n\n"]
)
def test_file_snapshot_restores_presence_and_exact_contents(tmp_path, content):
    path = tmp_path / "file with spaces"
    if content is not None:
        path.write_text(content)
    runtime = _ShellRuntime()
    saved = FileSnapshot.capture(runtime, "local", str(path))
    path.write_text("injected state\n")
    saved.restore(runtime, "local")
    saved.restore(runtime, "local")
    assert path.exists() is (content is not None)
    if content is not None:
        assert path.read_text() == content


def test_checked_exec_rejects_error_output():
    with pytest.raises(RuntimeError, match="Recovery command failed") as failure:
        checked_exec(_ShellRuntime(), "local", "printf PRIVATE_SNAPSHOT; false")
    assert "PRIVATE_SNAPSHOT" not in str(failure.value)


def test_route_lines_preserves_multipath_and_removes_readonly_flags():
    assert route_lines(
        "default proto static\n"
        "    nexthop via 192.0.2.1 dev eth0 weight 1 linkdown\n"
        "    nexthop via 192.0.2.2 dev eth0 weight 2 dead\n"
        "192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.10 offload\n"
    ) == [
        "default proto static nexthop via 192.0.2.1 dev eth0 weight 1 nexthop via 192.0.2.2 dev eth0 weight 2",
        "192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.10",
    ]


def test_checked_exec_accepts_daemon_logs_after_success_marker():
    class Runtime:
        def exec(self, node, command):
            return "service started\nNIKA_RECOVERY_COMMAND_OK\ndaemon ready\n"

    assert (
        checked_exec(Runtime(), "router", "service frr start")
        == "service started\ndaemon ready"
    )


def test_checked_exec_rejects_marker_inside_error_message():
    class Runtime:
        def exec(self, node, command):
            return "error executing printf NIKA_RECOVERY_COMMAND_OK\n"

    with pytest.raises(RuntimeError, match="Recovery command failed"):
        checked_exec(Runtime(), "router", "false")


def test_stopped_dhcp_server_does_not_hide_lost_client_state(monkeypatch):
    original = IPv4Snapshot("eth0", ("192.0.2.10/24",), (), ())
    saved = DHCPRecovery(
        FileSnapshot("/etc/dhcp/dhcpd.conf", ""),
        False,
        (("client", original, FileSnapshot("/etc/resolv.conf", "")),),
    )
    renewed = []
    runtime = SimpleNamespace(
        systemctl=lambda *args: None,
        process_not_running=lambda *args: True,
        renew_dhcp_leases=lambda clients: renewed.extend(clients),
    )
    monkeypatch.setattr(FileSnapshot, "restore", lambda *args: None)
    monkeypatch.setattr(
        IPv4Snapshot,
        "capture",
        classmethod(lambda cls, *args: IPv4Snapshot("eth0", (), (), ())),
    )
    assert saved.restore(runtime, "server") is False
    assert renewed == []
