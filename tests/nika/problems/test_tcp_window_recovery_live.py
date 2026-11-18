"""TCP recovery reconnects constrained flows and preserves healthy connections."""

from __future__ import annotations

import json
import shlex
import subprocess
import time

import pytest

from nika.mcp.session_context import SESSION_ID_ENV
from nika.net_env.verify import tcp_socket_stats
from nika.problems.support.polling import wait_until
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available, linux_vrf_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not docker_available() or not linux_vrf_available(),
        reason="Docker and Linux VRF required",
    ),
]


def test_existing_window_limited_transfer_reconnects_on_recovery(monkeypatch, tmp_path):
    sid = start_net_env(
        "enterprise_branch", "m", session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, sid)
    failure = None
    runtime = None
    evidence = {"session_id": sid}
    try:
        session = Session().load_running_session(session_id=sid)
        failure = prepare_failure(
            session,
            ["tcp_receive_window_limited"],
            {
                "host_name": "br1_corp_pc",
                "baseline_trials": "1",
                "iperf_duration_sec": "3",
            },
        )
        runtime = failure.problem.runtime
        receiver, control = "br1_corp_pc", "br1_corp_pc2"
        assert wait_until(
            lambda: all(
                any(
                    s.peer.endswith(":8080") and s.bytes_received > 4096
                    for s in tcp_socket_stats(runtime, host)
                )
                for host in (receiver, control)
            ),
            75,
        )
        original = {s.cookie for s in tcp_socket_stats(runtime, receiver)}
        control_before = {s.cookie for s in tcp_socket_stats(runtime, control)}
        evidence["original_cookies"] = sorted(original)
        evidence["control_cookies"] = sorted(control_before)
        failure.inject()
        assert failure.verify()["verified"]
        saved_max = failure.problem._original.rmem_max
        assert saved_max is None or saved_max.isdigit()
        cap = failure.problem._target_buffer_bytes
        workload = failure.problem.net_env.business_workload()
        url = next(
            p.args[0]
            for p in workload.processes
            if p.node == receiver and p.app == "file_sync"
        )
        pid = runtime.exec(
            receiver,
            "nohup curl -sS --retry 3 --retry-all-errors --retry-delay 1 "
            + shlex.quote(url)
            + " >/dev/null 2>&1 </dev/null & echo $!",
        ).strip()
        assert pid.isdigit(), pid

        def constrained():
            return [
                s
                for s in tcp_socket_stats(runtime, receiver)
                if s.cookie not in original
                and s.peer.endswith(":8080")
                and s.receive_buffer <= cap
            ]

        assert wait_until(constrained, 15)
        old = constrained()[0]
        started = time.monotonic()
        time.sleep(3)
        sample = next(s for s in constrained() if s.cookie == old.cookie)
        fault_bps = (
            (sample.bytes_received - old.bytes_received)
            * 8
            / (time.monotonic() - started)
        )
        evidence["fault_socket"] = vars(sample)
        evidence["fault_bps"] = fault_bps
        assert fault_bps > 0
        result = failure.recover()
        evidence["recovery"] = result
        assert result["details"]["remaining_connections"] == []
        assert old.cookie in {row["cookie"] for row in result["details"]["reconnected"]}
        assert wait_until(
            lambda: (
                old.cookie
                not in {s.cookie for s in tcp_socket_stats(runtime, receiver)}
            ),
            10,
        ), "The existing constrained connection survived recovery"

        def reconnected():
            return [
                s
                for s in tcp_socket_stats(runtime, receiver)
                if s.cookie not in original
                and s.peer.endswith(":8080")
                and s.receive_buffer > cap
            ]

        assert wait_until(reconnected, 15)
        new = reconnected()[0]
        started = time.monotonic()
        time.sleep(3)
        sample = next(s for s in reconnected() if s.cookie == new.cookie)
        recovered_bps = (
            (sample.bytes_received - new.bytes_received)
            * 8
            / (time.monotonic() - started)
        )
        evidence["recovered_socket"] = vars(sample)
        evidence["recovered_bps"] = recovered_bps
        assert recovered_bps > fault_bps * 2, evidence
        assert original.issubset(
            {s.cookie for s in tcp_socket_stats(runtime, receiver)}
        )
        assert control_before.issubset(
            {s.cookie for s in tcp_socket_stats(runtime, control)}
        )
        assert failure.recover()["verified"]
        assert new.cookie in {s.cookie for s in tcp_socket_stats(runtime, receiver)}
        evidence["inspection"] = subprocess.run(
            [
                "uv",
                "run",
                "nika",
                "exec",
                "--session_id",
                sid,
                receiver,
                "ss -tinemHO state established",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    finally:
        (tmp_path / "recovery.json").write_text(json.dumps(evidence, indent=2))
        try:
            if failure is not None:
                failure.recover()
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                assert not runtime.exists() and not runtime.has_leftover_resources()
