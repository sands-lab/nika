"""Always-on business workload that a scenario declares.

A scenario returns a :class:`BusinessWorkload` from
``NetworkEnvBase.business_workload()``: the processes its users and services
run, and what that traffic exercises. Every session start (``nika env run``,
benchmark trials, audits) launches it after lab verification and before any
inject, so healthy and fault cases carry the same traffic. The processes live
inside the lab nodes and end when the lab is undeployed.

Programs come from :mod:`nika.traffic.apps`; each is copied to
``/usr/local/bin/<command>`` on its node. ``exercises`` lets failures whose
symptom needs traffic (``ProblemBase.SYMPTOM_WORKLOAD``) keep to targets the
workload actually loads.
"""

from __future__ import annotations

import base64
import shlex
import time
import zlib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from nika.config import pkg_path
from nika.runtime.base import LabRuntime
from nika.utils.parallel import bounded_parallel_map

# Exercise kinds and their target tuples.
# EF real-time media and best-effort bulk share one egress; the media would
# overload best effort if it lost its marking:
# ``(router, egress_interface)``.
REALTIME_BULK_CONTENTION = "realtime_bulk_contention"
# Sustained TCP bulk transfers into a host: ``(receiver,)``.
BULK_DOWNLOAD = "bulk_download"
# Sustained TCP bulk transfers leaving a node interface, at least
# ``BULK_TRANSIT_MIN_KBPS`` together: ``(node, egress_interface)``.
BULK_TRANSIT = "bulk_transit"
BULK_TRANSIT_MIN_KBPS = 1500
# Persistent and bursty TCP sessions source-NATed by a router, with more
# concurrent connections to one destination than a small port pool holds:
# ``(nat_router, inside_prefix)``.
NAT_SESSIONS = "nat_sessions"
# Several workers answer one query at the same moment, so their replies
# converge on the receiver's last-hop egress port: ``(receiver,)``.
FAN_IN_BURSTS = "fan_in_bursts"

# Short-lived keep-alive sessions to a stateful L4 virtual IP, so new
# connections keep opening through the gateway: ``(gateway,)``.
VIP_KEEPALIVE_SESSIONS = "vip_keepalive_sessions"

# Shares of a router platform's per-interface send cap
# (``NetworkEnvBase.router_rate_limit_kbps``) that the business workload and a
# background traffic profile may use. The rest is headroom: a hub's single WAN
# port aggregates several tunnels, and tunnel encapsulation adds overhead.
BUSINESS_RATE_SHARE = 0.6
PROFILE_RATE_SHARE = 0.15


def rate_budget_kbps(limit_kbps: int | None, share: float) -> float | None:
    """The kbit/s ``share`` of ``limit_kbps``, or None when nothing caps the rate."""
    return None if limit_kbps is None else limit_kbps * share


INSTALL_DIR = "/usr/local/bin"
# Launch rounds for processes that are not up yet, and the pause between them.
START_ATTEMPTS = 3
START_RETRY_DELAY_SEC = 5.0


@dataclass(frozen=True)
class WorkloadProcess:
    """One long-running program on one node.

    ``app`` names a module in :mod:`nika.traffic.apps`; ``command`` is the
    installed name, unique per node.
    """

    node: str
    app: str
    command: str
    args: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return f"{INSTALL_DIR}/{self.command}"

    @property
    def command_line(self) -> str:
        return " ".join(shlex.quote(part) for part in (self.path, *self.args))


@dataclass(frozen=True)
class WorkloadExercise:
    """A traffic property the workload guarantees on ``target``."""

    kind: str
    target: tuple[str, ...]


@dataclass(frozen=True)
class BusinessWorkload:
    processes: tuple[WorkloadProcess, ...]
    exercises: tuple[WorkloadExercise, ...] = ()
    nodes: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        seen: set[tuple[str, str]] = set()
        for process in self.processes:
            key = (process.node, process.command)
            if key in seen:
                raise ValueError(f"duplicate workload command {key}")
            seen.add(key)
        object.__setattr__(
            self, "nodes", tuple(dict.fromkeys(p.node for p in self.processes))
        )

    def targets(self, kind: str) -> list[tuple[str, ...]]:
        return [e.target for e in self.exercises if e.kind == kind]

    def covers(self, kind: str, target: tuple[str, ...]) -> bool:
        return tuple(target) in self.targets(kind)


def pace_browsers(
    processes: Iterable[WorkloadProcess], rate_limit_kbps: int | None
) -> list[WorkloadProcess]:
    """Cap every ``browser`` transfer under a router send cap.

    The business share of the cap is split evenly over all browser users, as
    if every transfer crossed the same capped interface.
    """
    processes = list(processes)
    budget = rate_budget_kbps(rate_limit_kbps, BUSINESS_RATE_SHARE)
    browsers = [p for p in processes if p.app == "browser"]
    if not budget or not browsers:
        return processes

    def users(process: WorkloadProcess) -> int:
        args = list(process.args)
        return int(args[args.index("--users") + 1]) if "--users" in args else 1

    per_transfer = budget / sum(users(p) for p in browsers)
    return [
        WorkloadProcess(
            p.node,
            p.app,
            p.command,
            (*p.args, "--max-kbps", f"{per_transfer:.0f}"),
        )
        if p.app == "browser"
        else p
        for p in processes
    ]


def routine_change_processes(nodes: Iterable[str]) -> tuple[WorkloadProcess, ...]:
    """Benign operator edits on every FRR router in ``nodes``.

    Keeps each router's mgmtd commit history from holding only fault commits.
    """
    return tuple(
        WorkloadProcess(
            node=node,
            app="change_window",
            command="change-window",
            args=("--seed", str(zlib.crc32(f"{node}change-window".encode()) & 0xFFFF)),
        )
        for node in sorted(nodes)
    )


def _app_source(app: str) -> str:
    return pkg_path("traffic", "apps", f"{app}.py").read_text(encoding="utf-8")


def _running(runtime: LabRuntime, process: WorkloadProcess) -> bool:
    # Anchored on the interpreter so the probing shell never matches itself.
    pattern = f"^python3 {process.path}( |$)"
    out = runtime.exec(
        process.node, f"pgrep -f {shlex.quote(pattern)} >/dev/null && echo up"
    )
    return out.strip().endswith("up")


def _start_node(
    runtime: LabRuntime, processes: list[WorkloadProcess]
) -> list[WorkloadProcess]:
    """Install and launch one node's processes; return those not running."""
    launched = False
    for process in processes:
        if _running(runtime, process):
            continue
        encoded = base64.b64encode(_app_source(process.app).encode()).decode()
        runtime.exec(
            process.node,
            f"mkdir -p {INSTALL_DIR} && printf '%s' {encoded} | base64 -d "
            f"> {process.path} && chmod 755 {process.path}",
        )
        runtime.exec(
            process.node,
            f"setsid nohup {process.command_line} >/dev/null 2>&1 </dev/null &",
        )
        launched = True
    if launched:
        time.sleep(1.0)
    return [p for p in processes if not _running(runtime, p)]


def start_workload(runtime: LabRuntime, workload: BusinessWorkload) -> None:
    """Launch every process not already running.

    Idempotent, so reusing a deployed lab does not duplicate processes.
    Raises ``RuntimeError`` when a process does not stay up.
    """
    pending = list(workload.processes)
    failed: list[WorkloadProcess] = []
    # A loaded host can be slow to show a just-launched process; re-launch
    # the missing ones before giving up. _start_node skips running ones.
    for attempt in range(START_ATTEMPTS):
        if attempt:
            time.sleep(START_RETRY_DELAY_SEC)
        by_node: dict[str, list[WorkloadProcess]] = defaultdict(list)
        for process in pending:
            by_node[process.node].append(process)
        failed = [
            process
            for missing in bounded_parallel_map(
                lambda node: _start_node(runtime, by_node[node]), list(by_node)
            )
            for process in missing
        ]
        if not failed:
            break
        pending = failed
    if failed:
        names = ", ".join(f"{p.node}:{p.command}" for p in failed)
        raise RuntimeError(f"business workload did not start: {names}")


def _signal_processes(
    runtime: LabRuntime, paths: dict[str, set[str]], signal: str
) -> None:
    def send(node: str) -> None:
        alternatives = "|".join(sorted(paths[node]))
        pattern = f"^python3 ({alternatives})( |$)"
        runtime.exec(node, f"pkill -{signal} -f {shlex.quote(pattern)} || true")

    bounded_parallel_map(send, list(paths))


@contextmanager
def paused_workload(
    runtime: LabRuntime,
    workload: BusinessWorkload | None,
    settle_sec: float,
    hosts: Iterable[str] = (),
) -> Iterator[None]:
    """Hold the lab's traffic programs still while a measurement needs a quiet lab.

    For test-path probes that read whole-port counters. The workload's
    programs, and any case background traffic on ``hosts``
    (:mod:`nika.traffic.profiles`), stop (SIGSTOP); transfers already handed
    to the kernel get ``settle_sec`` to drain. They resume (SIGCONT)
    afterwards, also on error.
    """
    from nika.traffic.profiles import BACKGROUND_COMMANDS, POD_PROGRAMS

    paths: dict[str, set[str]] = defaultdict(set)
    for process in workload.processes if workload else ():
        paths[process.node].add(process.path)
    for host in hosts:
        paths[host].update(f"{INSTALL_DIR}/{c}" for c in BACKGROUND_COMMANDS)
        paths[host].update(POD_PROGRAMS)
    if not paths:
        yield
        return
    _signal_processes(runtime, paths, "STOP")
    try:
        time.sleep(settle_sec)
        yield
    finally:
        _signal_processes(runtime, paths, "CONT")
