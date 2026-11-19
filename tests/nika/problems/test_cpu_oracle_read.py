"""CPU contention evidence remains bounded to the archive batch's workers."""

import pytest

from nika.agents.oracle.agent import _observed
from nika.problems.endpoint_application.transport import _cpu_contention_steps

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("output", "confirmed"),
    [
        ("archive_children\n" + "gzip\n" * 16, True),
        ("archive_children\ngzip\n", False),
        ("archive_children\n", False),
        ("[TIMEOUT] archive_children\ngzip\ngzip\n", False),
        ("gzip\ngzip\n", False),
        ("archive_children\npython3\ngzip\ngzip-helper\n", False),
        ("archive_children\n gzip\n gzip\n", True),
        ("", False),
    ],
)
def test_archive_worker_evidence_needs_live_competing_processes(output, confirmed):
    step = _cpu_contention_steps("endpoint", quota_evidence=True)[0]
    assert _observed(step.expect, output) is confirmed


@pytest.mark.parametrize(
    "quota,throttled,expected",
    [("5000 100000", 3, True), ("max 100000", 3, False), ("5000 100000", 0, False)],
)
def test_cpu_quota_observation_uses_builtins_and_preserves_evidence(
    tmp_path, quota, throttled, expected
):
    import subprocess

    (tmp_path / "cpu.max").write_text(quota + "\n")
    (tmp_path / "cpu.stat").write_text(
        f"usage_usec 100\nnr_periods 7\nnr_throttled {throttled}\nthrottled_usec 200"
    )
    step = _cpu_contention_steps("endpoint", quota_evidence=True)[1]
    command = step.args["command"].replace("/sys/fs/cgroup", str(tmp_path))
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": "/nonexistent"},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert quota in result.stdout and f"nr_throttled {throttled}" in result.stdout
    assert _observed(step.expect, result.stdout) is expected
    assert step.evidence and step.attempts == 3 and step.args["timeout"] == 60


def test_cpu_quota_observation_rejects_an_unreadable_counter_file(tmp_path):
    import subprocess

    (tmp_path / "cpu.max").write_text("5000 100000\n")
    step = _cpu_contention_steps("endpoint", quota_evidence=True)[1]
    command = step.args["command"].replace("/sys/fs/cgroup", str(tmp_path))
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": "/nonexistent"},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Cannot read" in result.stderr and "cpu.stat" in result.stderr
    assert not _observed(step.expect, result.stdout + result.stderr)


@pytest.mark.parametrize(
    "children,expected",
    [
        ("7 8 9", True),
        ("7 8 404", True),
        ("404", False),
        ("7", False),
        ("9", False),
        ("", False),
    ],
)
def test_worker_observation_reads_only_live_archive_children(
    tmp_path, children, expected
):
    import subprocess

    parent = tmp_path / "proc/42/task/42"
    parent.mkdir(parents=True)
    # Linux's children file has no trailing newline.
    (parent / "children").write_text(children)
    pidfile = tmp_path / "archive.pid"
    pidfile.write_text("42\n")
    for pid, comm in [(7, "gzip"), (8, "gzip"), (9, "gzip-helper"), (99, "gzip")]:
        process = tmp_path / f"proc/{pid}"
        process.mkdir()
        (process / "comm").write_text(comm + "\n")
    step = _cpu_contention_steps("endpoint", quota_evidence=True)[0]
    command = step.args["command"].replace("/run/archive-job.pid", str(pidfile))
    command = command.replace("/proc/", str(tmp_path / "proc") + "/")
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": "/nonexistent"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.startswith("archive_children\n")
    assert _observed(step.expect, result.stdout) is expected
    assert step.evidence and step.attempts == 3 and step.args["timeout"] == 60


@pytest.mark.parametrize("pid", ["", "42", "not-a-pid"])
def test_worker_observation_rejects_missing_or_stale_archive_parent(tmp_path, pid):
    import subprocess

    pidfile = tmp_path / "archive.pid"
    pidfile.write_text(pid)
    step = _cpu_contention_steps("endpoint", quota_evidence=True)[0]
    command = step.args["command"].replace("/run/archive-job.pid", str(pidfile))
    command = command.replace("/proc/", str(tmp_path / "proc") + "/")
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": "/nonexistent"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert not _observed(step.expect, result.stdout)
