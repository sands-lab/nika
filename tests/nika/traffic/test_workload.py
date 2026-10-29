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
