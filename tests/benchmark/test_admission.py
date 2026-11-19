"""Host admission: capacity decision, spacing, wait events, and /proc readers."""

from __future__ import annotations

import pytest

from scripts.benchmark_audit import admission

pytestmark = pytest.mark.unit


def test_has_capacity_needs_memory_and_load_headroom() -> None:
    assert admission.has_capacity(10, 3, floor_gib=8, ceiling=4)
    assert not admission.has_capacity(7.9, 3, floor_gib=8, ceiling=4)
    assert not admission.has_capacity(10, 4, floor_gib=8, ceiling=4)


def test_proc_readers(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 1\nMemAvailable: 16777216 kB\n")
    loadavg = tmp_path / "loadavg"
    loadavg.write_text("1.50 2.00 3.25 2/900 1234\n")
    real_path = admission.Path
    monkeypatch.setattr(
        admission,
        "Path",
        lambda p: {"/proc/meminfo": meminfo, "/proc/loadavg": loadavg}.get(
            str(p), real_path(p)
        ),
    )
    assert admission.available_gib() == 16.0
    assert admission.load_average() == (1.5, 2.0, 3.25)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, sec: float) -> None:
        self.slept.append(sec)
        self.t += sec


def _wait(tmp_path, clock, loads, **kwargs):
    readings = iter(loads)
    events: list[dict] = []
    waited = admission.wait_for_admission(
        "light",
        lock_path=tmp_path / "a.lock",
        ceiling=8,
        read_memory=lambda: 64.0,
        read_load=lambda: next(readings),
        sleep=clock.sleep,
        now=clock.now,
        on_wait=events.append,
        **kwargs,
    )
    return waited, events


def test_waits_until_load_drops(tmp_path) -> None:
    clock = _Clock()
    waited, events = _wait(tmp_path, clock, [20.0, 12.0, 3.0])
    assert [e["load"] for e in events] == [20.0, 12.0]
    assert events[0]["ceiling"] == 8
    assert waited == 2 * admission.POLL_SEC


def test_spacing_after_previous_admission(tmp_path) -> None:
    clock = _Clock()
    (tmp_path / "a.lock").write_text(f"{clock.t - 4} clab")
    waited, events = _wait(tmp_path, clock, [1.0], spacing={"clab": 180})
    assert events == []
    assert clock.slept == [176.0]
    assert waited == 176.0


def test_max_wait_admits_anyway(tmp_path) -> None:
    clock = _Clock()
    waited, events = _wait(tmp_path, clock, [50.0] * 10, max_wait_sec=45)
    assert len(events) == 3
    assert waited >= 45


def test_native_audit_waits_for_full_lab_estimate(monkeypatch):
    from scripts.benchmark_audit import matrix
    from nika.net_env.net_env_pool import get_net_env_instance

    row = {
        "scenario": "isp_pdh",
        "device_profile": "iosxr",
        "backend": "kathara",
        "bgp_mode": "ibgp_rr",
    }
    env = get_net_env_instance(
        "isp_pdh", device_profile="iosxr", backend="kathara", bgp_mode="ibgp_rr"
    )
    required = env.native_lab_memory() / 1024**3
    assert required > matrix._MEMORY_FLOOR_GIB[matrix.effective_class(row)]
    captured = {}
    monkeypatch.setattr(
        admission, "wait_for_admission", lambda *args, **kwargs: captured.update(kwargs)
    )
    matrix.wait_for_slot(row)
    assert captured["floors"][matrix.effective_class(row)] == required


def test_native_memory_wait_rechecks_capacity(tmp_path):
    clock = _Clock()
    memory = iter([36.0, 36.5, 38.0])
    events = []
    waited = admission.wait_for_admission(
        "k8s",
        lock_path=tmp_path / "native.lock",
        ceiling=8,
        floors={"k8s": 38.0},
        read_memory=lambda: next(memory),
        read_load=lambda: 1,
        sleep=clock.sleep,
        now=clock.now,
        on_wait=events.append,
    )
    assert waited == 2 * admission.POLL_SEC
    assert [event["memory_gib"] for event in events] == [36.0, 36.5]


def test_audit_retries_when_native_capacity_drops_before_deploy(monkeypatch):
    from contextlib import nullcontext
    from scripts.benchmark_audit import matrix
    from nika.net_env.utils.native_admission import NativeLabMemoryError

    row = {"scenario": "isp_pdh", "device_profile": "iosxr", "problem": "healthy"}
    waits = []
    attempts = []
    audit = matrix._failed_audit(row, "test result")

    def run(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise NativeLabMemoryError("capacity changed")
        return audit

    monkeypatch.setattr(matrix, "_hold_class_slot", lambda cls: nullcontext())
    monkeypatch.setattr(matrix, "wait_for_slot", lambda row: waits.append(1))
    monkeypatch.setattr(matrix, "audit_case", run)
    monkeypatch.setattr(matrix, "case_image_mode", lambda: False)
    result = matrix.run_audit_row(row)
    assert len(attempts) == len(waits) == 2
    assert result["error"] is None
