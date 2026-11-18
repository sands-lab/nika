"""Real throughput probes preserve unrelated listeners and leave no artifacts."""

from __future__ import annotations

import json
import subprocess

import pytest

from nika.net_env.verify import iperf_tcp_metrics
from nika.runtime.factory import runtime_for_session
from nika.utils.session_store import SessionStore
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]


def test_throughput_probe_owns_its_server_and_leaves_no_files(tmp_path):
    sid = start_net_env(
        "simple_bgp", None, session_tag=resolve_session_tag(context="test")
    )
    runtime = None
    try:
        runtime = runtime_for_session(SessionStore().get_session(sid))
        peer = runtime.get_host_ip("pc2", "eth0")
        sentinel = "/tmp/iperf3_s_52431.log"
        runtime.write_file("pc2", sentinel, "existing owner data\n")
        normal = iperf_tcp_metrics(
            runtime, "pc1", "pc2", peer, duration_sec=1, port=52431
        )
        fresh = iperf_tcp_metrics(
            runtime, "pc1", "pc2", peer, duration_sec=1, port=52432
        )
        failed = iperf_tcp_metrics(
            runtime, "pc1", "pc2", "127.0.0.1", duration_sec=1, port=52434
        )
        runtime.exec(
            "pc2", "iperf3 -s -D -p 52433 -I /tmp/owned-server.pid >/dev/null 2>&1"
        )
        owner_pid = runtime.exec("pc2", "cat /tmp/owned-server.pid").strip()
        assert owner_pid.isdigit()
        occupied = iperf_tcp_metrics(
            runtime, "pc1", "pc2", peer, duration_sec=1, port=52433
        )
        inspection = subprocess.run(
            [
                "uv",
                "run",
                "nika",
                "exec",
                "--session_id",
                sid,
                "pc2",
                "ss -Hltnp; ls -l /tmp/iperf3* 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
        data = {
            "normal": normal,
            "fresh": fresh,
            "failed": failed,
            "occupied": occupied,
            "sentinel_after": runtime.exec("pc2", f"cat {sentinel}"),
            "fresh_log": runtime.exec(
                "pc2", "test -e /tmp/iperf3_s_52432.log && echo exists || true"
            ),
            "inspection": inspection,
        }
        (tmp_path / "probe-evidence.json").write_text(json.dumps(data, indent=2))
        assert normal[0] and fresh[0]
        assert failed == (None, None)
        assert data["sentinel_after"].strip() == "existing owner data"
        assert not data["fresh_log"].strip()
        assert occupied == (None, None)
        assert f"pid={owner_pid}," in inspection
        assert ":52433" in inspection
        assert all(f":{port}" not in inspection for port in (52431, 52432, 52434))
    finally:
        close_session(session_id=sid)
        if runtime is not None:
            assert not runtime.exists() and not runtime.has_leftover_resources()
