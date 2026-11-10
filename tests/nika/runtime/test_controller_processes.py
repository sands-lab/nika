"""Controller cleanup must never signal an unrelated host process."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from nika.runtime.shared.processes import (
    owned_pidfd,
    process_command,
    process_identity,
    run_in_node_netns,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def worker():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


@pytest.mark.parametrize("record", ["legacy", "ticks", "boot", "invalid"])
def test_stale_worker_record_does_not_signal_live_process(tmp_path, worker, record):
    identity = process_identity(worker.pid)
    if record == "legacy":
        identity = worker.pid
    elif record == "ticks":
        identity[1] += 1
    elif record == "boot":
        identity[2] = "different-host-boot"
    else:
        identity = [0, 0, ""]
    path = tmp_path / "worker.pid"
    path.write_text(json.dumps(identity))
    result = subprocess.run(
        process_command("stop", str(path)), capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert worker.poll() is None
    assert not path.exists()


def test_recorded_worker_can_be_checked_and_stopped(tmp_path, worker):
    path = tmp_path / "worker.pid"
    identity = process_identity(worker.pid)
    subprocess.run(process_command("record", str(path), str(worker.pid)), check=True)
    assert json.loads(path.read_text()) == process_identity(worker.pid)
    running = subprocess.run(
        process_command("running", str(path)),
        capture_output=True,
        text=True,
        check=True,
    )
    assert running.stdout.strip() == "running"
    subprocess.run(process_command("stop", str(path)), check=True)
    assert worker.wait(timeout=5) != 0
    assert not path.exists()
    assert owned_pidfd(identity) is None


def test_node_namespace_command_refuses_host_before_execution(tmp_path):
    marker = tmp_path / "executed"
    with pytest.raises(RuntimeError, match="host network namespace"):
        run_in_node_netns(
            os.getpid(), [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
        )
    assert not marker.exists()


def test_splice_payload_has_its_process_helpers_without_nika_installed():
    from nika.runtime.kathara.vde_proxy import _SPLICE_SOURCE

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import os, sys; scope = {'__name__': 'controller'}; "
            "exec(sys.stdin.read(), scope); "
            "print(scope['_PROCESS_HELPERS']['process_identity'](os.getpid())[0])",
        ],
        input=_SPLICE_SOURCE,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().isdigit()


def test_exited_group_leader_does_not_block_child_cleanup():
    from nika.runtime.shared.processes import stop_owned

    code = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(child.pid, flush=True); sys.stdin.readline()"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child = None
    try:
        identity = process_identity(leader.pid)
        child = process_identity(int(leader.stdout.readline()))
        leader.communicate("exit\n", timeout=5)
        assert owned_pidfd(identity) is None
        fd = owned_pidfd(child)
        assert fd is not None
        os.close(fd)
    finally:
        if leader.poll() is None:
            leader.kill()
        leader.wait(timeout=5)
        if child is not None:
            stop_owned(child)
