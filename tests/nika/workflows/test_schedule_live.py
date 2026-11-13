"""Real session isolation, cancellation, recovery, and timed burst traffic."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.prerequisites import docker_available
from tests.support.schedule_assertions import assert_schedule_timing

pytestmark = pytest.mark.e2e


@pytest.fixture
def fabric():
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.workflows.benchmark.inject_resolve import resolve_inject_params

    env = get_net_env_instance("dc_clos", topo_size="s", rendered=False)
    dns = resolve_inject_params("dns_record_error", "dc_clos", "s", seed=42)
    return env, dns


def _worker(root, tag, plan, start, ready, remote=None):
    if remote is not None:
        from nika.remote.config import remote_override

        with remote_override(remote):
            return _worker(root, tag, plan, start, ready)

    from nika.remote.config import is_remote_enabled
    from nika.remote.schedule import RemoteSessionScheduler
    from nika.schedule import normalize_schedule
    from nika.workflows.benchmark._trial_worker import _raise_on_sigterm
    from nika.workflows.env.start import start_net_env
    from nika.workflows.schedule import SessionScheduler
    from nika.workflows.session.close import close_session

    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    sid, scheduler = None, None
    try:
        sid = start_net_env("dc_clos", "s", instance_tag=tag, result_dir=root)
        scheduler_type = (
            RemoteSessionScheduler if is_remote_enabled() else SessionScheduler
        )
        scheduler = scheduler_type(sid, normalize_schedule(plan))
        scheduler.prepare()
        ready.put((sid, str(scheduler.directory)))
        if not start.wait(120):
            raise RuntimeError("Test controller did not release the session")
        scheduler.start()
        scheduler.wait_agent_start()
        while not scheduler.complete.is_set():
            scheduler.wait_for_progress(scheduler.observation_revision(), 0.1)
            scheduler.raise_if_failed()
        scheduler.raise_if_failed()
    finally:
        try:
            if scheduler is not None:
                scheduler.close()
        finally:
            if sid:
                close_session(
                    session_id=sid,
                    status="finished"
                    if scheduler is not None and scheduler.complete.is_set()
                    else "aborted",
                )


def _events(directory):
    path = Path(directory) / "schedule-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _wait(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("Scheduled runtime condition did not become true")


def _exec(sid, host, command):
    result = subprocess.run(
        ["uv", "run", "nika", "exec", "--session_id", sid, host, command],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return result.stdout


@pytest.mark.skipif(not docker_available(), reason="Docker required")
def test_remote_schedule_defers_business_traffic_and_restores_the_link(
    tmp_path, fabric
):
    env, dns = fabric
    client = env.model.client_endpoints()[0].name
    from nika.config import REPO_ROOT
    from nika.remote.client import RemoteClient
    from nika.remote.config import RemoteConfig, remote_override
    from nika.remote.schedule import RemoteSessionScheduler
    from nika.schedule import normalize_schedule
    from nika.utils.session_store import SessionStore
    from nika.workflows.agent.run import start_agent
    from nika.workflows.env.start import start_net_env
    from nika.workflows.session.close import close_session

    # A separate package root gives the daemon its own session store, as on a lab host.
    remote_root = tmp_path / "lab-host"
    shutil.copytree(
        REPO_ROOT / "src",
        remote_root / "src",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config_path = tmp_path / "native.yaml"
    config_path.write_text("nika:\n  remote:\n    enabled: false\n")
    env = {
        **os.environ,
        "PYTHONPATH": str(remote_root / "src"),
        "NIKA_RUN_CONFIG": str(config_path),
        "NIKA_STATE_DIR": str(remote_root),
    }
    sid = f"schedule-remote-{uuid4().hex[:8]}"
    remote_client = RemoteClient(
        RemoteConfig(enabled=True, url=f"http://127.0.0.1:{port}", artifact_root="")
    )
    scheduler = None
    with (tmp_path / "daemon.log").open("w") as log:
        daemon = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"from nika.remote.server import serve_remote; serve_remote(host='127.0.0.1', port={port})",
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:

            def reachable():
                try:
                    return remote_client.health().status == "ok"
                except RuntimeError:
                    return False

            _wait(reachable, 30)
            with remote_override(remote_client.config):
                start_net_env(
                    "dc_clos",
                    "s",
                    session_id=sid,
                    result_dir=str(tmp_path / "results"),
                    defer_business_traffic=True,
                )
                scheduler = RemoteSessionScheduler(
                    sid,
                    normalize_schedule(
                        {
                            "agent_start": 0,
                            "events": [
                                {"traffic": "business", "at": 2},
                                {
                                    "problem": "link_down",
                                    "inject": {
                                        "host_name": client,
                                        "intf_name": "eth0",
                                    },
                                    "at": 6,
                                    "duration": 10,
                                },
                            ],
                        },
                    ),
                )
                scheduler.prepare()
                remote_meta = remote_client.get_session(sid)
                assert remote_meta["session_dir"] != str(scheduler.directory)
                assert "browser" not in _exec(
                    sid,
                    client,
                    "pgrep -af '^python3 /usr/local/bin/browser' || true",
                )
                assert "200" in _exec(
                    sid,
                    client,
                    f"python3 -c \"import urllib.request; print(urllib.request.urlopen('http://{dns['target_website']}.{dns['target_domain']}/').status)\"",
                )

                def release():
                    scheduler.start()
                    scheduler.wait_agent_start()

                start_agent(
                    agent_type="mock",
                    session_id=sid,
                    diagnosis_ready=release,
                    check_fault_presence=True,
                    stream_output=False,
                )
                scheduler.raise_if_failed()
                assert scheduler.complete.is_set()
                assert scheduler.recheck_artifact()["not_applicable"]
                assert (
                    "UP"
                    in _exec(sid, client, "cat /sys/class/net/eth0/operstate").upper()
                )
                assert "browser" in _exec(
                    sid, client, "pgrep -af '^python3 /usr/local/bin/browser'"
                )
                scheduler.close()
                events = _events(scheduler.directory)
                assert_schedule_timing(scheduler.directory, agent=True)
                assert any(
                    e["status"] == "applied" and e.get("event_index") == 0
                    for e in events
                )
                assert any(e["status"] == "verified" for e in events)
                assert any(e["status"] == "recovered" for e in events)
                truth = json.loads(
                    (scheduler.directory / "ground_truth.json").read_text()
                )
                assert len(truth["root_causes"]) == 1
                messages = [
                    json.loads(line)
                    for line in (scheduler.directory / "messages.jsonl")
                    .read_text()
                    .splitlines()
                ]
                completed = next(
                    e["timestamp"] for e in events if e["status"] == "completed"
                )
                frozen = next(
                    e["timestamp"]
                    for e in messages
                    if e.get("event") == "diagnosis_frozen"
                )
                assert frozen > completed
                close_session(session_id=sid)
                with pytest.raises(FileNotFoundError):
                    SessionStore().get_session(sid)
                assert remote_client.list_sessions() == []
                from nika.runtime.factory import runtime_for_session

                runtime = runtime_for_session(remote_meta)
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()

            ctx = multiprocessing.get_context("spawn")
            ready, start = ctx.Queue(), ctx.Event()
            worker = ctx.Process(
                target=_worker,
                args=(
                    str(tmp_path / "cancelled"),
                    f"schedule-cancel-{uuid4().hex[:8]}",
                    {
                        "agent_start": 0,
                        "events": [
                            {
                                "problem": "link_down",
                                "inject": {
                                    "host_name": client,
                                    "intf_name": "eth0",
                                },
                                "at": 1,
                                "duration": 30,
                                "repeat": {"every": 60, "count": 2},
                            }
                        ],
                    },
                    start,
                    ready,
                    remote_client.config,
                ),
            )
            worker.start()
            cancelled_sid = None
            try:
                cancelled_sid, cancelled_dir = ready.get(timeout=180)
                cancelled_meta = remote_client.get_session(cancelled_sid)
                start.set()
                _wait(
                    lambda: (
                        "DOWN"
                        in _exec(
                            cancelled_sid,
                            client,
                            "cat /sys/class/net/eth0/operstate",
                        ).upper()
                    )
                )
                subprocess.run(
                    [
                        "uv",
                        "run",
                        "nika",
                        "session",
                        "close",
                        "--yes",
                        "--session_id",
                        cancelled_sid,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                worker.join(timeout=60)
                assert not worker.is_alive()
                assert any(
                    e["status"] == "cancelled" and e.get("occurrence_index") == 1
                    for e in _events(cancelled_dir)
                )
                assert remote_client.list_sessions() == []
                runtime = runtime_for_session(cancelled_meta)
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
            finally:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=60)
                if cancelled_sid and any(
                    row["session_id"] == cancelled_sid
                    for row in remote_client.list_sessions()
                ):
                    remote_client.close_session(cancelled_sid)
        finally:
            try:
                with remote_override(remote_client.config):
                    try:
                        if scheduler is not None:
                            scheduler.close()
                    finally:
                        try:
                            close_session(session_id=sid)
                        except FileNotFoundError:
                            pass
            finally:
                daemon.terminate()
                daemon.wait(timeout=30)


@pytest.mark.skipif(not docker_available(), reason="Docker required")
def test_default_initial_events_keep_distinct_targets_for_the_same_failure(
    tmp_path, fabric
):
    env, dns = fabric
    client = env.model.client_endpoints()[0].name
    dns_host = dns["host_name"]
    from nika.runtime.factory import runtime_for_session
    from nika.workflows.session.close import close_session

    ctx = multiprocessing.get_context("spawn")
    ready, start = ctx.Queue(), ctx.Event()
    plan = {
        "events": [
            {"problem": "link_down", "inject": {"host_name": host, "intf_name": "eth0"}}
            for host in (client, dns_host)
        ]
    }
    worker = ctx.Process(
        target=_worker,
        args=(str(tmp_path), f"schedule-initial-{uuid4().hex[:8]}", plan, start, ready),
    )
    sid, directory = None, None
    worker.start()
    try:
        sid, directory = ready.get(timeout=180)
        materialized = json.loads((Path(directory) / "schedule.json").read_text())
        assert (
            materialized["events"][0]["root_causes"]
            != materialized["events"][1]["root_causes"]
        )
        start.set()
        worker.join(timeout=90)
        assert not worker.is_alive() and worker.exitcode == 0
        truth = json.loads((Path(directory) / "ground_truth.json").read_text())
        assert len(truth["root_causes"]) == 2
        assert sum(e["status"] == "verified" for e in _events(directory)) == 2
        assert_schedule_timing(directory)
        runtime = runtime_for_session(
            json.loads((Path(directory) / "run.json").read_text())
        )
        assert not runtime.exists() and not runtime.has_leftover_resources()
    finally:
        start.set()
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=60)
        if sid:
            close_session(session_id=sid, session_dir=directory)


@pytest.mark.skipif(not docker_available(), reason="Docker required")
def test_timed_dns_recovery_preserves_another_active_failure(tmp_path, fabric):
    env, dns = fabric
    client = env.model.client_endpoints()[0].name
    dns_host = dns["host_name"]
    from nika.runtime.factory import runtime_for_session
    from nika.workflows.session.close import close_session

    ctx = multiprocessing.get_context("spawn")
    ready, start = ctx.Queue(), ctx.Event()
    plan = {
        "agent_start": 0,
        "events": [
            {
                "problem": "dns_record_error",
                "inject": {
                    **dns,
                    "wrong_ip": "203.0.113.99",
                },
                "at": 1,
                "duration": 8,
                "repeat": {"every": 16, "count": 2},
            },
            {
                "problem": "link_down",
                "inject": {"host_name": client, "intf_name": "eth0"},
                "at": 2,
                "duration": 31,
            },
        ],
    }
    worker = ctx.Process(
        target=_worker,
        args=(str(tmp_path), f"schedule-dns-{uuid4().hex[:8]}", plan, start, ready),
    )
    sid, directory = None, None
    worker.start()
    try:
        sid, directory = ready.get(timeout=180)
        query = f"dig +short +time=2 +tries=1 @127.0.0.1 {dns['target_website']}.{dns['target_domain']} A"
        baseline = _exec(sid, dns_host, query).strip()
        assert baseline and "203.0.113.99" not in baseline
        start.set()
        for occurrence in range(2):
            for status in ("verified", "recovered"):
                _wait(
                    lambda: any(
                        e["status"] == status
                        and e.get("event_index") == 0
                        and e.get("occurrence_index") == occurrence
                        for e in _events(directory)
                    )
                )
                answer = _exec(sid, dns_host, query).strip()
                assert answer == ("203.0.113.99" if status == "verified" else baseline)
            assert "down" in _exec(sid, client, "cat /sys/class/net/eth0/operstate")
        worker.join(timeout=60)
        assert not worker.is_alive() and worker.exitcode == 0
        truth = json.loads((Path(directory) / "ground_truth.json").read_text())
        assert len(truth["root_causes"]) == 2
        assert_schedule_timing(directory)
        runtime = runtime_for_session(
            json.loads((Path(directory) / "run.json").read_text())
        )
        assert not runtime.exists() and not runtime.has_leftover_resources()
    finally:
        start.set()
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=60)
        if sid:
            close_session(session_id=sid, session_dir=directory)


@pytest.mark.skipif(not docker_available(), reason="Docker required")
def test_session_timing_cancellation_and_bursts_are_isolated(tmp_path, fabric):
    env, dns = fabric
    client = env.model.client_endpoints()[0].name
    dns_host = dns["host_name"]
    web = next(host for host in env.servers["web"] if host != dns_host)
    from nika.utils.session_store import SessionStore
    from nika.runtime.factory import runtime_for_session
    from nika.workflows.session.close import close_session

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Queue()
    starts = [ctx.Event(), ctx.Event()]
    failure = {
        "problem": "link_down",
        "inject": {"host_name": client, "intf_name": "eth0"},
    }
    plans = [
        {
            "agent_start": 0,
            "events": [
                {
                    **failure,
                    "at": 1,
                    "duration": 30,
                    "repeat": {"every": 40, "count": 2},
                }
            ],
        },
        {
            "agent_start": 4,
            "events": [
                {
                    **failure,
                    "at": 12,
                    "duration": 5,
                    "repeat": {"every": 12, "count": 2},
                },
                {
                    "traffic": {
                        "type": "burst",
                        "sources": [dns_host],
                        "destination": web,
                        "protocol": "udp",
                        "rate": "100K",
                    },
                    "at": 1,
                    "duration": 6,
                    "repeat": {"every": 10, "count": 2},
                },
            ],
        },
    ]
    token = uuid4().hex[:8]
    workers = [
        ctx.Process(
            target=_worker,
            args=(str(tmp_path), f"schedule-{token}-{i}", plans[i], starts[i], ready),
        )
        for i in range(2)
    ]
    sessions = []
    try:
        for worker in workers:
            worker.start()
        sessions = [ready.get(timeout=180) for _ in workers]
        # Readiness order can differ from worker order; identify plans by artifact.
        sessions.sort(
            key=lambda item: len(
                json.loads((Path(item[1]) / "schedule.json").read_text())["events"]
            )
        )
        sid_a, directory_a = sessions[0]
        sid_b, directory_b = sessions[1]
        starts[0].set()
        _wait(lambda: any(e["status"] == "verified" for e in _events(directory_a)))
        assert (
            "DOWN" in _exec(sid_a, client, "cat /sys/class/net/eth0/operstate").upper()
        )
        assert "UP" in _exec(sid_b, client, "cat /sys/class/net/eth0/operstate").upper()
        starts[1].set()
        _wait(
            lambda: any(
                e["status"] == "applied" and e.get("event_index") == 1
                for e in _events(directory_b)
            )
        )
        _wait(
            lambda: "connected to" in _exec(sid_b, dns_host, "cat /tmp/burst-*.log"),
            timeout=10,
        )
        traffic = _exec(sid_b, dns_host, "cat /tmp/burst-*.log")
        assert "iperf3: error" not in traffic
        assert "connected to" in traffic
        _wait(
            lambda: any(
                float(value) > 0
                for value in re.findall(
                    r"(\d+(?:\.\d+)?)\s+[KMG]?Bytes",
                    _exec(sid_b, web, "cat /tmp/iperf-server-*.log"),
                )
            ),
            timeout=10,
        )
        closed = subprocess.run(
            ["uv", "run", "nika", "session", "close", "--yes", "--session_id", sid_a],
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        assert closed.returncode == 0, closed.stdout + closed.stderr
        workers[0].join(timeout=60)
        assert not workers[0].is_alive()
        assert any(
            e["status"] == "cancelled" and e.get("occurrence_index") == 1
            for e in _events(directory_a)
        )
        with pytest.raises(FileNotFoundError):
            SessionStore().get_session(sid_a)
        _wait(
            lambda: any(
                e["status"] == "recovered" and e.get("event_index") == 0
                for e in _events(directory_b)
            )
        )
        assert "UP" in _exec(sid_b, client, "cat /sys/class/net/eth0/operstate").upper()
        workers[1].join(timeout=90)
        assert workers[1].exitcode == 0
        rows = _events(directory_b)
        assert len([e for e in rows if e["status"] == "verified"]) == 2
        assert (
            len(
                [
                    e
                    for e in rows
                    if e["status"] == "applied" and e.get("event_index") == 1
                ]
            )
            == 2
        )
        assert len([e for e in rows if e["status"] == "recovered"]) == 4
        assert all(e["session_id"] == sid_b for e in rows)
        assert (
            len(
                json.loads((Path(directory_b) / "ground_truth.json").read_text())[
                    "root_causes"
                ]
            )
            == 1
        )
        assert any(e["status"] == "completed" for e in rows)
        assert_schedule_timing(directory_b)
        for _, directory in sessions:
            runtime = runtime_for_session(
                json.loads((Path(directory) / "run.json").read_text())
            )
            assert not runtime.exists()
            assert not runtime.has_leftover_resources()
    finally:
        for start in starts:
            start.set()
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=60)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=10)
        for sid, directory in sessions:
            run = json.loads((Path(directory) / "run.json").read_text())
            status = run.get("status")
            close_session(
                session_id=sid,
                session_dir=directory,
                status=status
                if status in {"finished", "error", "aborted"}
                else "aborted",
            )
