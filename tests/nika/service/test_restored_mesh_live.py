"""Restored healthy handoff requires readable endpoint monitoring."""

import json
import subprocess
import time

import pytest

from nika.net_env.isp.containerlab.lab import SRL_IMAGE
from nika.net_env.probe_intent import ProbeIntent
from nika.net_env.utils.iosxr.common import IMAGE as IOSXR_IMAGE
from nika.net_env.utils.routeros.common import IMAGE as ROUTEROS_IMAGE
from nika.net_env.utils.sonic.common import IMAGE as SONIC_IMAGE
from nika.net_env.verify import verify_lab_with_retry
from nika.problems.support.polling import wait_until
from nika.remote.handlers import handle_session_verify
from nika.runtime.factory import runtime_for_session
from nika.service.pingmesh.recorded import recorded_mesh
from nika.service.pingmesh.baseline import read_latency_baseline
from nika.telemetry import telemetry_running
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session, session_net_env
from scripts.benchmark_audit.agent_view import run_agent_view, session_host_api
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_available,
    docker_image_available,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "scenario,backend,profile,node,image",
    [
        ("dc_clos", "kathara", "frr", "client_1_1", None),
        ("dc_clos", "containerlab", "sonic", "client_1_1", SONIC_IMAGE),
        ("dc_clos", "containerlab", "nokia_srlinux", "client_1_1", SRL_IMAGE),
        ("enterprise_branch", "kathara", "routeros", "br1_corp_pc", ROUTEROS_IMAGE),
        ("enterprise_branch", "kathara", "iosxr", "br1_corp_pc", IOSXR_IMAGE),
    ],
)
@pytest.mark.skipif(not docker_available(), reason="Requires Docker")
def test_healthy_handoff_rejects_unavailable_prober_and_recovers(
    tmp_path, monkeypatch, scenario, backend, profile, node, image
):
    if backend == "containerlab" and not containerlab_prerequisites():
        pytest.skip("Requires Containerlab and gnmic")
    if image is not None and not docker_image_available(image):
        pytest.skip(f"Requires the {profile} image")
    sid = start_net_env(
        scenario,
        "s",
        backend=backend,
        device_profile=profile,
        telemetry=["pingmesh"],
        traffic={"pattern": "uniform", "load_mbps": 1.0},
        session_tag=resolve_session_tag(context="test"),
    )
    (tmp_path / "session.json").write_text(
        json.dumps({"session_id": sid, "profile": profile})
    )
    runtime = None
    stopped = None
    observations = []
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)
        api, meta = session_host_api(sid)
        env = session_net_env(meta)
        intent = ProbeIntent.from_metadata(meta["metadata"])
        baselines = read_latency_baseline(meta["session_dir"])
        assert handle_session_verify(sid)["verified"]
        observations.append(
            {
                "phase": "healthy",
                "mesh": recorded_mesh(api, intent=intent, baselines=baselines),
            }
        )
        state = subprocess.run(
            [
                "nika",
                "exec",
                "--session_id",
                sid,
                "--timeout",
                "15",
                node,
                "ip -br address; ip route; ss -lnt",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        observations.append({"phase": "nika exec", "output": state.stdout})
        stopped = runtime.exec(
            node, "pgrep -f '^prometheus-blackbox-exporter --config.file='"
        ).strip()
        assert stopped.isdigit(), stopped
        runtime.exec(node, f"kill -STOP {stopped}")

        def unavailable():
            return not telemetry_running(runtime, meta["metadata"]["telemetry"])

        assert wait_until(unavailable, 60, interval_s=3)
        # The data plane remains healthy while this monitoring process is stopped.
        assert verify_lab_with_retry(env, full=True)["verified"]
        observations.append(
            {
                "phase": "unavailable",
                "mesh": recorded_mesh(api, intent=intent, baselines=baselines),
            }
        )
        started = time.monotonic()
        with monkeypatch.context() as patch:
            patch.setattr("nika.telemetry.telemetry_ready_timeout", lambda _plan: 20)
            with pytest.raises(RuntimeError, match="Pingmesh is not ready"):
                handle_session_verify(sid)
        assert not telemetry_running(runtime, meta["metadata"]["telemetry"])
        assert runtime.exec(node, f"ps -o stat= -p {stopped}").strip().startswith("T")
        observations.append(
            {"phase": "rejected", "elapsed_sec": time.monotonic() - started}
        )
        runtime.exec(node, f"kill -CONT {stopped}")
        stopped = None
        assert handle_session_verify(sid)["verified"]
        recovered = recorded_mesh(api, intent=intent, baselines=baselines)
        assert not recovered["anomalies"], recovered
        view = run_agent_view(sid, env, scenario, "s")
        assert not view["violated_count"] and not view["error_count"], view
        observations.append(
            {"phase": "recovered", "mesh": recovered, "agent_view": view}
        )
    finally:
        (tmp_path / "observations.json").write_text(json.dumps(observations, indent=2))
        try:
            if stopped is not None and runtime is not None:
                runtime.exec(node, f"kill -CONT {stopped}")
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
