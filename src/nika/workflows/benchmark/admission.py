"""Host-load admission shared by benchmark trials and the release audit.

A lab deploy claims memory and CPU for minutes. Starting several at once pushes
the host load far above its core count, and healthy labs then show high latency
or transient loss. ``wait_for_admission`` admits one lab at a time while free
memory stays above a per-class floor and the 1-minute load average stays under
a ceiling, then records the admission so the next lab waits out the class
spacing before it reads memory.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import IO, Callable, Mapping

MEMORY_FLOOR_GIB: Mapping[str, float] = {"light": 8, "large": 16, "clab": 32, "k8s": 16}
# SR Linux nodes keep claiming memory for minutes after a clab lab deploys.
SPACING_SEC: Mapping[str, float] = {"light": 10, "large": 45, "clab": 180, "k8s": 90}
RESOURCE_CLASSES = tuple(MEMORY_FLOOR_GIB)

POLL_SEC = 20.0
DEFAULT_LOCK_DIR = "/tmp/nika-admission-locks"


def available_gib() -> float:
    """Free memory in GiB from ``/proc/meminfo`` (``MemAvailable``)."""
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def load_average() -> tuple[float, float, float]:
    """1, 5 and 15 minute load averages from ``/proc/loadavg``."""
    one, five, fifteen = Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3]
    return float(one), float(five), float(fifteen)


def load_ceiling(per_cpu: float) -> float:
    return per_cpu * (os.cpu_count() or 1)


def has_capacity(
    memory_gib: float, load: float, *, floor_gib: float, ceiling: float
) -> bool:
    return memory_gib >= floor_gib and load < ceiling


def wait_for_admission(
    resource_class: str,
    *,
    lock_path: Path,
    ceiling: float,
    floors: Mapping[str, float] = MEMORY_FLOOR_GIB,
    spacing: Mapping[str, float] = SPACING_SEC,
    max_wait_sec: float = 0,
    on_wait: Callable[[dict], None] | None = None,
    poll_sec: float = POLL_SEC,
    read_memory: Callable[[], float] = available_gib,
    read_load: Callable[[], float] = lambda: load_average()[0],
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> float:
    """Block until the host can take one more lab of ``resource_class``.

    Admissions are serialized through ``lock_path``, which records the last
    admission. The lock is released between checks so a waiting heavy lab does
    not block smaller ones that still fit. ``max_wait_sec`` > 0 admits anyway
    after that long. Returns the seconds waited; ``on_wait`` gets each refusal.
    """
    floor = floors[resource_class]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = now()
    while True:
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            lock.seek(0)
            last = lock.read().split()
            if len(last) == 2 and last[1] in spacing:
                settle = float(last[0]) + spacing[last[1]] - now()
                if settle > 0:
                    sleep(settle)
            memory, load = read_memory(), read_load()
            timed_out = max_wait_sec > 0 and now() - started >= max_wait_sec
            if timed_out or has_capacity(
                memory, load, floor_gib=floor, ceiling=ceiling
            ):
                lock.seek(0)
                lock.truncate()
                lock.write(f"{now()} {resource_class}")
                lock.flush()
                return now() - started
        if on_wait is not None:
            on_wait(
                {
                    "resource_class": resource_class,
                    "waited_sec": round(now() - started, 1),
                    "memory_gib": round(memory, 1),
                    "floor_gib": floor,
                    "load": round(load, 1),
                    "ceiling": round(ceiling, 1),
                }
            )
        sleep(poll_sec)


def hold_class_slot(
    resource_class: str,
    *,
    slots: Mapping[str, int],
    lock_path: Path,
    poll_sec: float = POLL_SEC,
) -> IO[str]:
    """Block until one of the class's slots is free; the slot lasts until the file closes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        for index in range(slots[resource_class]):
            handle = Path(f"{lock_path}.{resource_class}.{index}").open("w")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            return handle
        time.sleep(poll_sec)
