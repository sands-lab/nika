"""A host address fault persists through real DHCP renewal and recovers cleanly."""

from __future__ import annotations

import time
import subprocess

import pytest

from nika.mcp.session_context import SESSION_ID_ENV
from nika.utils.session import Session
from nika.runtime.factory import runtime_for_session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not docker_available(), reason="Docker required for DHCP")
def test_incorrect_ip_survives_dhcp_and_restores_renewal(monkeypatch):
    session_id = start_net_env(
        "campus_lan", "s", session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, session_id)
    failure = None
    host = "pc_1_1_1_1"
    hook = "/etc/dhcp/dhclient-enter-hooks.d/address"
    try:
        session = Session().load_running_session(session_id=session_id)
        failure = prepare_failure(
            session,
            ["host_incorrect_ip"],
            dict(host_name=host, incorrect_ip="192.0.2.77/24"),
        )
        runtime = failure.problem.runtime
        original = runtime.get_host_ip(host, "eth0", with_prefix=True)
        runtime.write_file(host, hook, "# Existing address policy\n")
        failure.inject()
        # The client keeps running. Its next normal lease renewal must retain
        # the configured wrong address, rather than silently healing the case.
        deadline = time.monotonic() + 90
        while True:
            address = runtime.exec(host, "ip -4 -o addr show dev eth0 scope global")
            assert "192.0.2.77/24" in address, address
            if "dynamic" in address:
                break
            assert time.monotonic() < deadline, "No natural DHCP renewal observed"
            time.sleep(2)
        assert failure.verify()["verified"]
        runtime.renew_dhcp_leases([host])
        assert failure.verify()["verified"]
        assert "192.0.2.77/24" in runtime.get_host_ip(host, "eth0", with_prefix=True)
        assert failure.recover()["verified"]
        failure = None
        assert runtime.exec(host, f"cat {hook}").strip() == "# Existing address policy"
        runtime.renew_dhcp_leases([host])
        assert runtime.get_host_ip(host, "eth0", with_prefix=True) == original
        assert "default via 10.1.1.1" in runtime.exec(host, "ip -4 route show default")
        assert "0% packet loss" in runtime.exec(host, "ping -c 2 -W 2 10.200.0.3")
    finally:
        try:
            if failure is not None:
                failure.recover()
        finally:
            close_session(session_id=session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker required for DHCP")
def test_missing_subnet_removes_stale_lease_backup_and_recovers(monkeypatch, tmp_path):
    session_id = start_net_env(
        "campus_lan", "s", session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, session_id)
    runtime = None
    failure = None
    try:
        session = Session().load_running_session(session_id=session_id)
        runtime = runtime_for_session(session)
        host = "pc_1_1_1_1"
        original = runtime.get_host_ip(host, "eth0", with_prefix=True)
        assert original.startswith("10.1.1.")
        # dhcpd keeps the previous lease database when rewriting it. Reproduce
        # that normal precondition with the real, populated healthy database.
        runtime.exec(
            "dhcp_server",
            "cp /var/lib/dhcp/dhcpd.leases /var/lib/dhcp/dhcpd.leases~",
        )
        before = runtime.exec("dhcp_server", "cat /var/lib/dhcp/dhcpd.leases~")
        assert original.split("/")[0] in before
        failure = prepare_failure(
            session,
            ["dhcp_missing_subnet"],
            {"host_name": "dhcp_server", "host_name_2": host, "subnet": "10.1.1.0"},
        )
        failure.inject()
        assert failure.verify()["verified"]
        inspection = subprocess.run(
            [
                "uv",
                "run",
                "nika",
                "exec",
                "--session_id",
                session_id,
                "dhcp_server",
                "cat /etc/dhcp/dhcpd.conf; "
                "test ! -e /var/lib/dhcp/dhcpd.leases~ && echo NO_STALE_BACKUP",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        (tmp_path / "dhcp-fault-inspection.txt").write_text(inspection)
        assert "NO_STALE_BACKUP" in inspection
        assert "subnet 10.1.1.0 netmask" not in inspection
        assert failure.recover()["verified"]
        failure = None
        assert runtime.get_host_ip(host, "eth0", with_prefix=True) == original
        assert "0% packet loss" in runtime.exec(host, "ping -c 2 -W 2 10.200.0.3")
    finally:
        try:
            if failure is not None:
                failure.recover()
        finally:
            close_session(session_id=session_id)
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
