import time

import pytest

from nika.traffic.workload import BusinessWorkload, WorkloadProcess, start_workload

pytestmark = pytest.mark.unit


class _UpRuntime:
    def exec(self, node, command, timeout=None):
        if "pgrep" in command:
            return "up"
        raise AssertionError(f"unexpected exec: {command}")


def test_start_workload_returns_once_processes_are_up(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    start_workload(
        _UpRuntime(),
        BusinessWorkload(processes=(WorkloadProcess("pc1", "file_sync", "filesync"),)),
    )
    assert slept == []


def test_browsers_share_the_business_part_of_a_send_cap() -> None:
    from nika.traffic.workload import pace_browsers

    processes = [
        WorkloadProcess("pc1", "browser", "browser", ("http://a/", "--users", "2")),
        WorkloadProcess("pc1", "browser", "downloader", ("http://a/large.bin",)),
        WorkloadProcess("pc1", "file_sync", "filesync", ("http://a/x",)),
    ]
    assert pace_browsers(processes, None) == processes
    paced = pace_browsers(processes, 1000)
    assert [p.args[-2:] for p in paced[:2]] == [("--max-kbps", "200")] * 2
    assert paced[2] == processes[2]
