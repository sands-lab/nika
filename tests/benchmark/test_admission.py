"""Host admission: capacity decision, spacing, wait events, and /proc readers."""

from __future__ import annotations

import pytest

from nika.run_config.schema import RunConfig
from nika.workflows.benchmark import admission

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


def test_benchmark_defaults_and_validation() -> None:
    cfg = RunConfig().benchmark.admission
    assert cfg.enabled and cfg.load_per_cpu == 1.0
    assert cfg.memory_floor_gib == dict(admission.MEMORY_FLOOR_GIB)
    with pytest.raises(ValueError):
        RunConfig.model_validate(
            {"benchmark": {"admission": {"spacing_sec": {"x": 1}}}}
        )
