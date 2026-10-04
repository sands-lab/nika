"""Opt-in resource profiling of one NIKA session, split by lifecycle phase.

``ResourceSampler`` appends periodic samples to ``<session_dir>/resources.jsonl``:
lab containers (cgroup v2 counters), the NIKA process tree that drives the session,
and host context. ``summarize`` slices those samples into phases using the
lifecycle events in ``<session_dir>/nika.jsonl`` and writes ``resources.json``.

The files are result artifacts only; they are never exposed to agents.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SAMPLES_FILENAME = "resources.jsonl"
SUMMARY_FILENAME = "resources.json"
EVENTS_FILENAME = "nika.jsonl"

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CLK_TCK = os.sysconf("SC_CLK_TCK")
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")
_DOCKER_DAEMONS = ("dockerd", "containerd")


@dataclass(frozen=True)
class PhaseSpec:
    """A phase window derived from existing lifecycle events.

    The window ends at the first ``end`` event and starts its ``duration_ms``
    before it. ``until`` events after the end extend the window (trailing
    bookkeeping). Overlapping windows resolve to the higher ``priority``.
    """

    name: str
    end: tuple[str, ...]
    until: tuple[str, ...] = ()
    priority: int = 2


PHASES: tuple[PhaseSpec, ...] = (
    # env_start spans the whole start; convergence takes precedence inside it.
    PhaseSpec(
        "deploy",
        ("env_start", "env_start_failed", "env_verify_failed", "env_start_interrupted"),
        priority=1,
    ),
    PhaseSpec("convergence", ("env_verify",)),
    PhaseSpec(
        "inject",
        ("failure_inject_complete", "failure_inject_error", "failure_verify_failed"),
    ),
    PhaseSpec("cleanup", ("env_stop", "env_stop_failed"), until=("session_cleared",)),
)
OTHER_PHASE = "other"


def _read_int(path: Path) -> int:
    return int(path.read_text().split()[0])


def _read_keyed(path: Path) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in (line.split() for line in path.read_text().splitlines())
    }


def _io_bytes(path: Path) -> tuple[int, int]:
    read = write = 0
    for line in path.read_text().splitlines():
        for field in line.split()[1:]:
            key, _, value = field.partition("=")
            if key == "rbytes":
                read += int(value)
            elif key == "wbytes":
                write += int(value)
    return read, write


def _proc_cpu_ns(pid: int) -> int:
    """Process CPU including reaped children (utime, stime, cutime, cstime)."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return sum(int(value) for value in fields[11:15]) * 1_000_000_000 // _CLK_TCK


def _proc_rss(pid: int) -> int:
    return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * _PAGE_SIZE


def _process_tree(root: int) -> list[int]:
    pids, stack = [], [root]
    while stack:
        pid = stack.pop()
        pids.append(pid)
        try:
            for task in Path(f"/proc/{pid}/task").iterdir():
                stack.extend(
                    int(child) for child in (task / "children").read_text().split()
                )
        except OSError:
            continue
    return pids


def _docker_daemon_pids() -> list[int]:
    pids = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if (entry / "comm").read_text().strip() in _DOCKER_DAEMONS:
                    pids.append(int(entry.name))
            except OSError:
                continue
    return pids


class _Counter:
    """Per-key cumulative counters turned into non-negative deltas."""

    def __init__(self) -> None:
        self._last: dict[Any, int] = {}

    def delta(self, key: Any, value: int, *, first: int = 0) -> int:
        previous = self._last.get(key)
        self._last[key] = value
        if previous is None:
            return first
        return max(0, value - previous)

    def seen(self, key: Any) -> bool:
        return key in self._last

    def forget(self, key: Any) -> None:
        self._last.pop(key, None)


class ResourceSampler:
    """Sample a session's lab containers, NIKA processes, and host context.

    Containers are discovered through the session (``list_session_containers``)
    and then read from their cgroups until they disappear, so teardown stays
    measured after the session record is cleared. Discovery backs off to
    ``max_discover_interval`` while the container set is stable and stops once
    the session is no longer running, to keep load off the Docker daemon. A container's whole cgroup
    usage is counted when first seen, because session containers are created by
    the session; it is kept in ``cpu_initial_ns`` so it does not inflate the
    per-interval CPU rate. ``process_root`` is the NIKA process driving the session
    (default: this process); its tree is the framework cost.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str | Path,
        *,
        interval: float = 1.0,
        discover_interval: float = 5.0,
        max_discover_interval: float = 60.0,
        process_root: int | None = None,
    ) -> None:
        self.session_id = session_id
        self.path = Path(session_dir) / SAMPLES_FILENAME
        self.interval = interval
        self.discover_interval = discover_interval
        self.max_discover_interval = max_discover_interval
        self.process_root = process_root or os.getpid()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._cgroups: dict[str, Path] = {}
        self._discover_errors: list[str] = []
        self._discover_cpu_ns = 0
        self._counters = _Counter()

    def __enter__(self) -> ResourceSampler:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Discovery waits on the Docker API, which can stall under load; a
        # separate thread keeps the sampling interval steady meanwhile.
        self._threads = [
            threading.Thread(
                target=target, name=f"{name}-{self.session_id}", daemon=True
            )
            for target, name in (
                (self._run_samples, "resource-sampler"),
                (self._run_discovery, "resource-discovery"),
            )
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join()
        self._threads = []

    def _run_samples(self) -> None:
        daemons = _docker_daemon_pids()
        with self.path.open("a", encoding="utf-8") as out:
            while True:
                stopping = self._stop.is_set()
                out.write(json.dumps(self._sample(daemons)) + "\n")
                out.flush()
                if stopping:
                    break
                self._stop.wait(self.interval)

    def _run_discovery(self) -> None:
        import docker

        client = docker.from_env(timeout=10)
        backoff = self.discover_interval
        running_seen = False
        try:
            while not self._stop.is_set():
                changing = self._discover(client)
                self._discover_cpu_ns = time.thread_time_ns()
                if changing is None:
                    # Closed after running: nothing new appears; known cgroups
                    # are still read. Not registered yet: retry soon.
                    if running_seen:
                        return
                    wait = self.interval
                else:
                    running_seen = True
                    # Back off only once containers exist and the set is stable.
                    with self._lock:
                        empty = not self._cgroups
                    backoff = (
                        self.discover_interval
                        if changing or empty
                        else min(backoff * 2, self.max_discover_interval)
                    )
                    wait = backoff
                self._stop.wait(wait)
        finally:
            client.close()

    def _discover(self, client: Any) -> int | None:
        """Register new session containers.

        Returns how many containers were added or are still unresolved, or
        ``None`` when the session is not running.
        """
        from docker.errors import NotFound

        from nika.runtime.factory import resolve_backend
        from nika.utils.session_store import SessionStore
        from nika.workflows.session.containers import list_session_containers

        try:
            session = SessionStore().get_session(self.session_id)
        except FileNotFoundError:
            return None
        if session.get("status") != "running":
            return None
        try:
            if resolve_backend(session) == "containerlab" and session.get("lab_name"):
                # ``clab inspect`` lists nodes only once deploy finishes; the
                # lab label is set when each container is created.
                container_ids = [
                    container.short_id
                    for container in client.containers.list(
                        all=True,
                        sparse=True,
                        filters={"label": f"containerlab={session['lab_name']}"},
                    )
                ]
            else:
                container_ids = [
                    row["container_id"]
                    for row in list_session_containers(self.session_id)[2]
                ]
        except Exception as exc:  # noqa: BLE001 - e.g. Docker API timeout under load
            self._discover_error(f"discover: {exc}")
            return 1  # unknown state: keep polling at the base interval
        added = unresolved = 0
        for container_id in container_ids:
            with self._lock:
                if container_id in self._cgroups:
                    continue
            try:
                pid = client.containers.get(container_id).attrs["State"]["Pid"]
                if not pid:
                    unresolved += 1
                    continue
                relative = (
                    Path(f"/proc/{pid}/cgroup").read_text().strip().split("::", 1)[1]
                )
            except NotFound:
                continue  # removed since listing (teardown)
            except Exception as exc:  # noqa: BLE001 - container may vanish mid-start
                unresolved += 1
                self._discover_error(f"cgroup {container_id}: {exc}")
                continue
            with self._lock:
                self._cgroups[container_id] = _CGROUP_ROOT / relative.lstrip("/")
            added += 1
        return added + unresolved

    def _discover_error(self, message: str) -> None:
        with self._lock:
            self._discover_errors.append(message)

    def _sample(self, daemons: list[int]) -> dict[str, Any]:
        sample: dict[str, Any] = {"t": time.time()}
        with self._lock:
            cgroups = list(self._cgroups.items())
            errors, self._discover_errors = self._discover_errors, []
        memory = working_set = pids = cpu = cpu_initial = io_read = io_write = 0
        for container_id, root in cgroups:
            try:
                usage = _read_int(root / "memory.current")
                inactive = _read_keyed(root / "memory.stat").get("inactive_file", 0)
                container_cpu = _read_keyed(root / "cpu.stat")["usage_usec"] * 1000
                container_pids = _read_int(root / "pids.current")
                read, write = _io_bytes(root / "io.stat")
            except FileNotFoundError:
                with self._lock:
                    self._cgroups.pop(container_id, None)
                for metric in ("cpu", "io_read", "io_write"):
                    self._counters.forget((metric, container_id))
                continue
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"read {container_id}: {exc}")
                continue
            memory += usage
            working_set += max(0, usage - inactive)
            pids += container_pids
            if self._counters.seen(("cpu", container_id)):
                cpu += self._counters.delta(("cpu", container_id), container_cpu)
            else:
                self._counters.delta(("cpu", container_id), container_cpu)
                cpu_initial += container_cpu
            io_read += self._counters.delta(("io_read", container_id), read, first=read)
            io_write += self._counters.delta(
                ("io_write", container_id), write, first=write
            )
        with self._lock:
            containers = len(self._cgroups)
        sample.update(
            containers=containers,
            memory_bytes=memory,
            working_set_bytes=working_set,
            pids=pids,
            cpu_ns=cpu,
            cpu_initial_ns=cpu_initial,
            io_read_bytes=io_read,
            io_write_bytes=io_write,
        )

        framework_cpu = framework_rss = 0
        tree = _process_tree(self.process_root)
        for pid in tree:
            try:
                framework_cpu += _proc_cpu_ns(pid)
                framework_rss += _proc_rss(pid)
            except (OSError, IndexError, ValueError):
                continue
        docker_cpu = 0
        for pid in daemons:
            try:
                docker_cpu += _proc_cpu_ns(pid)
            except OSError:
                continue
        load1 = os.getloadavg()[0]
        meminfo = dict(
            line.split(":", 1)
            for line in Path("/proc/meminfo").read_text().splitlines()
        )
        sample.update(
            framework_cpu_ns=self._counters.delta("framework", framework_cpu),
            framework_rss_bytes=framework_rss,
            framework_processes=len(tree),
            docker_cpu_ns=self._counters.delta("docker", docker_cpu),
            sampler_cpu_ns=self._counters.delta(
                "sampler", time.thread_time_ns() + self._discover_cpu_ns
            ),
            host_load1=load1,
            host_mem_available_bytes=int(meminfo["MemAvailable"].split()[0]) * 1024,
        )
        if errors:
            sample["errors"] = errors
        return sample


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def phase_windows(
    events: list[dict[str, Any]], phases: tuple[PhaseSpec, ...] = PHASES
) -> list[tuple[float, float, int, str]]:
    """Return ``(start, end, priority, phase)`` windows from lifecycle events."""
    timed = []
    for event in events:
        try:
            stamp = datetime.fromisoformat(event["timestamp"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        timed.append((stamp, event))
    timed.sort(key=lambda item: item[0])
    windows = []
    for spec in phases:
        for index, (stamp, event) in enumerate(timed):
            if event.get("event") not in spec.end:
                continue
            duration = event.get("duration_ms")
            if duration is None:
                break
            end = stamp
            for later, other in timed[index + 1 :]:
                if other.get("event") in spec.until:
                    end = later
                elif other.get("event") in spec.end:
                    break
            windows.append(
                (stamp - float(duration) / 1000, end, spec.priority, spec.name)
            )
            break
    return windows


def phase_at(stamp: float, windows: list[tuple[float, float, int, str]]) -> str:
    best: tuple[int, float, str] | None = None
    for start, end, priority, name in windows:
        if start <= stamp <= end and (best is None or (priority, start) > best[:2]):
            best = (priority, start, name)
    return best[2] if best else OTHER_PHASE


def _phase_seconds(windows: list[tuple[float, float, int, str]]) -> dict[str, float]:
    edges = sorted({edge for start, end, _, _ in windows for edge in (start, end)})
    seconds: dict[str, float] = {}
    for left, right in zip(edges, edges[1:]):
        name = phase_at((left + right) / 2, windows)
        if name != OTHER_PHASE:
            seconds[name] = seconds.get(name, 0.0) + right - left
    return seconds


def _aggregate(rows: list[tuple[dict[str, Any], float]]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0}
    peak_cpu = max(
        (row["cpu_ns"] / 1e9 / dt * 100 for row, dt in rows if dt > 0), default=0.0
    )
    return {
        "samples": len(rows),
        "peak_memory_bytes": max(row["memory_bytes"] for row, _ in rows),
        "peak_working_set_bytes": max(row["working_set_bytes"] for row, _ in rows),
        "cpu_seconds": sum(
            row["cpu_ns"] + row.get("cpu_initial_ns", 0) for row, _ in rows
        )
        / 1e9,
        "peak_cpu_percent": round(peak_cpu, 1),
        "io_read_bytes": sum(row["io_read_bytes"] for row, _ in rows),
        "io_write_bytes": sum(row["io_write_bytes"] for row, _ in rows),
        "peak_pids": max(row["pids"] for row, _ in rows),
        "peak_containers": max(row["containers"] for row, _ in rows),
        "framework_cpu_seconds": sum(row["framework_cpu_ns"] for row, _ in rows) / 1e9,
        "framework_peak_rss_bytes": max(row["framework_rss_bytes"] for row, _ in rows),
        "docker_cpu_seconds": sum(row["docker_cpu_ns"] for row, _ in rows) / 1e9,
        "peak_host_load1": max(row["host_load1"] for row, _ in rows),
        "min_host_mem_available_bytes": min(
            row["host_mem_available_bytes"] for row, _ in rows
        ),
    }


def summarize(session_dir: str | Path, *, write: bool = True) -> dict[str, Any]:
    """Slice ``resources.jsonl`` into lifecycle phases; optionally write ``resources.json``."""
    root = Path(session_dir)
    samples = _read_jsonl(root / SAMPLES_FILENAME)
    events = _read_jsonl(root / EVENTS_FILENAME)
    windows = phase_windows(events)
    seconds = _phase_seconds(windows)

    by_phase: dict[str, list[tuple[dict[str, Any], float]]] = {}
    previous = None
    for sample in samples:
        dt = sample["t"] - previous if previous is not None else 0.0
        previous = sample["t"]
        by_phase.setdefault(phase_at(sample["t"], windows), []).append((sample, dt))

    phases = {}
    for name in [spec.name for spec in PHASES] + [OTHER_PHASE]:
        if name not in seconds and name not in by_phase:
            continue
        phases[name] = {
            "seconds": seconds.get(name),
            **_aggregate(by_phase.get(name, [])),
        }
    every = [row for rows in by_phase.values() for row in rows]
    link_count = next(
        (
            (event.get("data") or {}).get("link_count")
            for event in events
            if event.get("event") == "env_start"
        ),
        None,
    )
    summary = {
        "samples": len(samples),
        "sampling_errors": sum(len(sample.get("errors", [])) for sample in samples),
        "wall_seconds": samples[-1]["t"] - samples[0]["t"] if samples else None,
        "link_count": link_count,
        "sampler_cpu_seconds": sum(s.get("sampler_cpu_ns", 0) for s in samples) / 1e9,
        "total": _aggregate(every),
        "phases": phases,
    }
    if write:
        (root / SUMMARY_FILENAME).write_text(json.dumps(summary, indent=2) + "\n")
    return summary
