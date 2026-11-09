"""Session-owned wall-clock event execution and observation completion."""

from __future__ import annotations

import json
import ctypes
import errno
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nika.problems.rca import ProblemGroundTruth
from nika.problems.rca.materialize import assert_root_causes_match
from nika.schedule import Burst, Schedule, ScheduledAction
from nika.utils.logger import bind_session_dir, log_event
from nika.utils.session import Session
from nika.utils.session_artifacts import json_safe, write_json_atomic
from nika.utils.session_log_summaries import summarize_injection
from nika.workflows.failure.inject import (
    PreparedFailure,
    prepare_failure,
    _extract_injection_params,
    validate_failure_effect,
)


class ScheduleError(RuntimeError):
    """A required scheduled operation failed or missed its active window."""


_RUNNERS: dict[str, SessionScheduler] = {}
_RUNNERS_LOCK = threading.Lock()


def _process_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def write_schedule_owner(directory: Path, session_id: str) -> None:
    handler = signal.getsignal(signal.SIGTERM)
    if getattr(handler, "__module__", "") == "nika.workflows.benchmark._trial_worker":
        write_json_atomic(
            directory / "schedule-owner.json",
            {
                "session_id": session_id,
                "pid": os.getpid(),
                "process_start": _process_start(os.getpid()),
            },
        )


def cancel_session_schedule(meta: dict[str, Any]) -> bool:
    """Drain an owning worker before external session teardown."""
    sid = str(meta.get("session_id") or "")
    with _RUNNERS_LOCK:
        local = _RUNNERS.get(sid)
    if local is not None:
        try:
            local.close()
        except ScheduleError as exc:
            log_event("schedule_cleanup_error", str(exc), session_id=sid)
        return False
    directory = meta.get("session_dir")
    if not directory:
        return False
    owner_path = Path(directory) / "schedule-owner.json"
    try:
        owner = json.loads(owner_path.read_text())
    except FileNotFoundError:
        return False
    pid = owner["pid"]
    if (
        owner.get("session_id") != sid
        or pid == os.getpid()
        or not owner.get("process_start")
        or _process_start(pid) != owner.get("process_start")
    ):
        return False
    from nika.utils.session_store import SessionStore

    # The worker's existing SIGTERM handler runs scheduler and session cleanup.
    # Some Python builds omit pidfd wrappers even when libc supports them.
    libc = ctypes.CDLL(None, use_errno=True)
    libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    libc.pidfd_send_signal.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    descriptor = libc.pidfd_open(pid, 0)
    if descriptor < 0:
        error = ctypes.get_errno()
        if error == errno.ESRCH:
            return False
        raise OSError(error, "Cannot open schedule worker process handle")
    try:
        if _process_start(pid) != owner.get("process_start"):
            return False
        if libc.pidfd_send_signal(descriptor, signal.SIGTERM, None, 0) < 0:
            error = ctypes.get_errno()
            if error == errno.ESRCH:
                return False
            raise OSError(error, "Cannot interrupt schedule worker")
    finally:
        os.close(descriptor)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            SessionStore().get_session(sid)
        except FileNotFoundError:
            return True
        if _process_start(pid) != owner.get("process_start"):
            return False
        time.sleep(0.1)
    raise ScheduleError(
        f"Session {sid} worker did not finish cancellation within 60 seconds"
    )


def scheduler_for(session_id: str) -> SessionScheduler | None:
    from nika.mcp.gateway.session_registry import resolve_canonical_session_id

    canonical = resolve_canonical_session_id(session_id)
    with _RUNNERS_LOCK:
        return _RUNNERS.get(canonical)


class SessionScheduler:
    """One plan, clock, cancellation scope, and journal for an explicit session."""

    def __init__(
        self,
        session_id: str,
        schedule: Schedule,
        *,
        traffic: Any = None,
        expected_root_causes: list | None = None,
        restored: bool = False,
    ):
        self.session = Session().load_running_session(session_id=session_id)
        self.session_id = session_id
        self.directory = Path(self.session.session_dir)
        self.plan = schedule
        self.traffic = traffic
        self.expected_root_causes = expected_root_causes
        self.restored = restored
        self._initial_failure: PreparedFailure | None = None
        self._initial_futures: list[Any] = []
        self.cancelled = threading.Event()
        self.complete = threading.Event()
        self._agent_dispatch_ready = threading.Event()
        self._changed = threading.Condition()
        self._revision = 0
        self._journal_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._prepared: dict[tuple[int, int], PreparedFailure] = {}
        self._active: dict[tuple[int, int], None] = {}
        self._scopes: dict[tuple[int, int], set[str]] = {}
        self._recovered_at: dict[tuple[int, int], float] = {}
        self._failure_ids: dict[tuple[int, int], int] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._truth: dict[tuple[str, str], Any] = {}
        self._domains: dict[str, str] = {}
        self._bursts: dict[tuple[int, int], Any] = {}
        self._background_started = False
        self._background = None
        self._dispatched: set[ScheduledAction] = set()
        self.t0 = 0.0

    def prepare(self) -> None:
        """Validate capabilities, resolve targets, and prepare before starting time."""
        with self._close_lock:
            if self._closed:
                raise ScheduleError("This schedule has already closed")
            self._prepare()

    def _prepare(self) -> None:
        from nika.problems.context import init_problem
        from nika.remote.config import is_remote_enabled

        if is_remote_enabled():
            raise ValueError("Scheduled trials require a local lab controller")
        write_schedule_owner(self.directory, self.session_id)
        self.net_env, self.runtime = init_problem(
            self.session.scenario_name, **self.session.scenario_params
        )
        if self.restored and not self.plan.static:
            raise ScheduleError("Restored injections require persistent initial events")
        if self.plan.static and self.plan.events:
            names = [e.problem for e in self.plan.events]
            if self.restored:
                from nika.validation.presence import bound_injected_problem

                bound = bound_injected_problem(self.session_id)
                if bound is None:
                    raise ScheduleError("Restored injection instances are unavailable")
                self._initial_failure = PreparedFailure(*bound)
            else:
                overrides = (
                    self.plan.events[0].inject
                    if len(names) == 1
                    else {e.problem: e.inject for e in self.plan.events}
                )
                self._initial_failure = prepare_failure(self.session, names, overrides)
                if len(names) != len(set(names)):
                    from nika.problems.multi import MultiFaultParams

                    initial = self._initial_failure
                    labels = [
                        f"{summarize_injection(e.problem, resolved_params=e.inject)} [{i + 1}]"
                        for i, e in enumerate(self.plan.events)
                    ]
                    initial.params = MultiFaultParams(
                        sub_params={
                            label: fault.resolve_params(event.inject)
                            for label, fault, event in zip(
                                labels,
                                initial.problem.sub_faults,
                                self.plan.events,
                                strict=True,
                            )
                        }
                    )
                    initial.problem.problem_names = labels
                    initial.problem._resolved_params = initial.params
            for key, value in self._initial_failure.problem.taxonomy_metadata().items():
                self.session.update_session(key, value)
        if self.plan.activates("business"):
            workload = self.net_env.business_workload()
            if workload is None or not any(
                p.traffic_source for p in workload.processes
            ):
                raise ValueError("This environment declares no business workload")
        windows = []
        materialized_events = list(self.plan.events)
        for action in self.plan.actions():
            if action.stopping:
                continue
            event = self.plan.events[action.event_index]
            key = (action.event_index, action.occurrence_index)
            if event.problem:
                if self._initial_failure is not None:
                    from nika.problems.multi import MultiFaultProblem

                    initial = self._initial_failure
                    prepared = (
                        PreparedFailure(
                            initial.problem.sub_faults[action.event_index],
                            initial.params.sub_params[
                                initial.problem.problem_names[action.event_index]
                            ],
                        )
                        if isinstance(initial.problem, MultiFaultProblem)
                        else initial
                    )
                else:
                    prepared = prepare_failure(
                        self.session, [event.problem], event.inject
                    )
                if not prepared.problem.is_compatible(
                    self.session.scenario_name,
                    self.session.scenario_params.get("device_profile"),
                    backend=self.runtime.backend,
                ):
                    raise ValueError(
                        f"Scheduled failure {event.problem!r} is incompatible with this deployment"
                    )
                truth = prepared.problem.get_ground_truth()
                self._domains[event.problem] = truth.failure_domain
                if event.root_causes is not None:
                    assert_root_causes_match(truth, event.root_causes)
                from nika.problems.rca import canonical_root_causes

                materialized_events[action.event_index] = event.model_copy(
                    update={"root_causes": canonical_root_causes(truth.root_causes)}
                )
                scopes = {cause.resource_id for cause in truth.root_causes}
                if not scopes:
                    raise ValueError(
                        f"Scheduled failure {event.problem!r} has no resolved mutation scope"
                    )
                self._prepared[key] = prepared
                record = {
                    "session_id": self.session_id,
                    "problem_name": event.problem,
                    **prepared.problem.taxonomy_metadata(),
                    "scenario_name": self.session.scenario_name,
                    "lab_name": self.session.lab_name,
                    "event_index": action.event_index,
                    "occurrence_index": action.occurrence_index,
                    "injection_params": {
                        **_extract_injection_params(prepared.problem),
                        "resolved_params": json_safe(
                            prepared.params.model_dump(exclude_none=True)
                            if prepared.params is not None
                            else {}
                        ),
                    },
                    "status": "pending",
                }
                if self.restored:
                    existing = self.session.store.list_failure_injections(
                        session_id=self.session_id
                    )
                    matches = [
                        i
                        for i, row in enumerate(existing)
                        if row.get("problem_name") == event.problem
                    ]
                    if len(matches) != 1:
                        raise ScheduleError(
                            "Restored failure records do not match the initial events"
                        )
                    self._failure_ids[key] = matches[0]
                    self.session.store.update_failure_injection(
                        self.session_id,
                        matches[0],
                        {
                            "session_id": self.session_id,
                            "event_index": action.event_index,
                            "occurrence_index": action.occurrence_index,
                            "restored": True,
                        },
                    )
                else:
                    self._failure_ids[key] = (
                        self.session.store.create_failure_injection(record)
                    )
                self._scopes[key] = scopes
                end = (
                    action.at + event.duration
                    if event.duration is not None
                    else float("inf")
                )
                for start2, end2, scopes2 in windows:
                    if (
                        not self.plan.static
                        and action.at < end2
                        and start2 < end
                        and scopes & scopes2
                    ):
                        raise ValueError(
                            f"Scheduled failures overlap on {sorted(scopes & scopes2)}"
                        )
                windows.append((action.at, end, scopes))
                for scope in scopes:
                    self._locks.setdefault(scope, threading.RLock())
            elif isinstance(event.traffic, Burst):
                from nika.traffic.burst import BurstTrafficGenerator

                generator = BurstTrafficGenerator(self.runtime)
                self._bursts[key] = generator.prepare(
                    sources=event.traffic.sources,
                    destination=event.traffic.destination,
                    protocol=event.traffic.protocol,
                    seed=event.traffic.seed
                    + action.event_index * 10000
                    + action.occurrence_index,
                )
        if self.plan.activates("background") and self.traffic is None:
            raise ValueError(
                "Scheduled background activation requires a traffic profile"
            )
        if self.plan.activates("background"):
            from nika.traffic.profiles import (
                normalize_traffic,
                prepare_background_traffic,
            )

            self._background = prepare_background_traffic(
                self.net_env, normalize_traffic(self.traffic)
            )
        self.plan = self.plan.model_copy(update={"events": materialized_events})
        self.session.update_session("schedule", self.plan.case_value())
        names = list(dict.fromkeys(e.problem for e in self.plan.events if e.problem))
        if self.expected_root_causes is not None:
            expected = {}
            for prepared in self._prepared.values():
                for cause in prepared.problem.get_ground_truth().root_causes:
                    expected[cause.pair_key()] = cause
            assert_root_causes_match(
                ProblemGroundTruth(
                    is_anomaly=bool(expected),
                    root_causes=list(expected.values()),
                ),
                self.expected_root_causes,
            )
        self.session.update_session(
            "problem_names", names or (["healthy"] if self.plan.static else [])
        )
        from nika.workflows.benchmark.healthy import healthy_task_description

        self.session.update_session(
            "task_description",
            self._initial_failure.problem.get_task_description()
            if self._initial_failure is not None
            else healthy_task_description(self.net_env)
            + (
                "\n\nObserve network changes during this task."
                "\nReport faults observed over time, including faults that recover."
                if not self.plan.static
                else ""
            ),
        )
        self.session.update_session(
            "failure_domain",
            self._domains[names[0]]
            if len(names) == 1
            else "multiple_faults"
            if names
            else "",
        )
        self._write_truth()
        write_json_atomic(self.directory / "schedule.json", self.plan.case_value())

    def _write_truth(self) -> None:
        fault_types = {cause.fault_type for cause in self._truth.values()}
        gt = ProblemGroundTruth(
            is_anomaly=bool(self._truth),
            root_causes=list(self._truth.values()),
            failure_domain="multiple_faults"
            if len(fault_types) > 1
            else self._domains.get(next(iter(fault_types), ""), ""),
        )
        self.session.write_gt(gt.model_dump(mode="json", exclude_none=True))

    def _record(
        self, status: str, action: ScheduledAction | None = None, **data: Any
    ) -> None:
        with self._journal_lock:
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": self.session_id,
                "status": status,
                "elapsed": time.monotonic() - self.t0 if self.t0 else None,
                **data,
            }
            if action is not None:
                event = self.plan.events[action.event_index]
                record.update(
                    event_index=action.event_index,
                    occurrence_index=action.occurrence_index,
                    operation="stop" if action.stopping else "start",
                    planned_offset=action.at,
                    lateness_sec=(
                        max(0.0, record["elapsed"] - action.at)
                        if record["elapsed"] is not None
                        else None
                    ),
                    summary=event.summary,
                )
            with (self.directory / "schedule-events.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(json.dumps(json_safe(record), ensure_ascii=False) + "\n")
            write_json_atomic(
                self.directory / "schedule-status.json",
                {
                    "completed": self.complete.is_set(),
                    "cancelled": self.cancelled.is_set() and not self.complete.is_set(),
                    "error": str(self._error) if self._error is not None else None,
                },
            )
            log_event(
                "schedule_event",
                f"Schedule {status}: session={self.session_id}, {record.get('summary', '')}"
                + (
                    f"; at={action.at:g}s; occurrence={action.occurrence_index + 1}; operation={record['operation']}"
                    if action
                    else ""
                ),
                **json_safe(record),
            )

    def _notify(self) -> None:
        with self._changed:
            self._revision += 1
            self._changed.notify_all()

    def start(self) -> None:
        with self._close_lock:
            self._start()

    def _start(self) -> None:
        with _RUNNERS_LOCK:
            if self.session_id in _RUNNERS and _RUNNERS[self.session_id] is not self:
                raise ValueError("This session already has an active scheduler")
            _RUNNERS[self.session_id] = self
        if self._thread is not None or self.cancelled.is_set():
            raise ValueError("This schedule has already started or closed")
        self.t0 = time.monotonic()
        self._record("started", t0_monotonic=self.t0)
        self._thread = threading.Thread(
            target=self._dispatch, name=f"schedule-{self.session_id}", daemon=True
        )
        self._thread.start()

    def status(self) -> dict[str, Any]:
        return {
            "completed": self.complete.is_set(),
            "cancelled": self.cancelled.is_set() and not self.complete.is_set(),
            "error": str(self._error) if self._error is not None else None,
            "revision": self.observation_revision(),
        }

    def _dispatch(self) -> None:
        bind_session_dir(self.directory)
        actions = self.plan.actions()
        self._pool = ThreadPoolExecutor(max_workers=max(2, min(32, len(actions))))
        futures = []
        tails = {}
        try:
            for action in actions:
                if action.at > (self.plan.agent_start or 0):
                    self._agent_dispatch_ready.set()
                if self.cancelled.wait(
                    max(0.0, self.t0 + action.at - time.monotonic())
                ):
                    break
                self._record("dispatched", action)
                self._dispatched.add(action)
                key = (action.event_index, action.occurrence_index)
                scopes = self._scopes.get(key, {f"traffic/{action.event_index}"})
                if key in self._prepared:
                    # Failure injection aligns saved configs across the whole lab.
                    scopes = scopes | {"saved-config-artifacts"}
                dependencies = list({tails[s] for s in scopes if s in tails})
                future = self._pool.submit(self._execute, action, dependencies)
                for scope in scopes:
                    tails[scope] = future
                futures.append(future)
                if action.at == 0:
                    self._initial_futures.append(future)
            self._agent_dispatch_ready.set()
            for future in futures:
                future.result()
            if not self.cancelled.is_set():
                self.complete.set()
                self._record("completed")
        except BaseException as exc:
            self._fail(exc)
        finally:
            self._agent_dispatch_ready.set()
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._notify()

    def _fail(self, error: BaseException) -> None:
        with self._journal_lock:
            if self._error is None:
                self._error = error
                self._record("failed", error=str(error))
        self.cancelled.set()
        self._notify()

    def _execute(self, action: ScheduledAction, dependencies: list[Any]) -> None:
        bind_session_dir(self.directory)
        key = (action.event_index, action.occurrence_index)
        event = self.plan.events[action.event_index]
        try:
            for dependency in dependencies:
                dependency.result()
            with ExitStack() as stack:
                for scope in sorted(self._scopes.get(key, ())):
                    stack.enter_context(self._locks[scope])
                if self.cancelled.is_set():
                    self._record("cancelled", action)
                    return
                self._record("executing", action)
                if action.stopping:
                    result = self._stop(key)
                    self._record("recovered", action, verification=result)
                elif event.problem:
                    previous = (action.event_index, action.occurrence_index - 1)
                    if (
                        event.repeat
                        and action.occurrence_index > 0
                        and event.duration < event.repeat.every
                        and self._recovered_at.get(previous, float("inf"))
                        > self.t0 + action.at
                    ):
                        raise ScheduleError(
                            "Previous recovery missed the next occurrence"
                        )
                    if (
                        event.duration is not None
                        and time.monotonic() >= self.t0 + action.at + event.duration
                    ):
                        raise ScheduleError(
                            "Failure activation missed its entire scheduled window"
                        )
                    prepared = self._prepared[key]
                    if not self.restored:
                        self._active[key] = None
                        self.session.store.update_failure_injection(
                            self.session_id,
                            self._failure_ids[key],
                            {"start_time": time.time()},
                        )
                        prepared.inject()
                    self._record(
                        "restored" if self.restored else "applied",
                        action,
                        resolved_params=json_safe(
                            prepared.params.model_dump(exclude_none=True)
                            if prepared.params is not None
                            else {}
                        ),
                    )
                    if (
                        event.duration is not None
                        and time.monotonic() >= self.t0 + action.at + event.duration
                    ):
                        raise ScheduleError(
                            "Failure mutation completed after its scheduled window"
                        )
                    if self.restored:
                        check = prepared.problem.recheck_artifact(prepared.params)
                        verification = {
                            "verified": bool(check.get("present")),
                            "artifact": check,
                        }
                    else:
                        verification = prepared.verify()
                    if not verification.get("verified"):
                        raise ScheduleError(
                            f"Scheduled injection verification failed: {verification}"
                        )
                    prepared.problem.net_env.align_saved_configs()
                    if (
                        event.duration is not None
                        and time.monotonic() >= self.t0 + action.at + event.duration
                    ):
                        raise ScheduleError(
                            "Injection verification exceeded its fixed recovery boundary"
                        )
                    truth = prepared.problem.get_ground_truth()
                    if event.root_causes is not None:
                        assert_root_causes_match(truth, event.root_causes)
                    with self._journal_lock:
                        for cause in truth.root_causes:
                            self._truth[cause.pair_key()] = cause
                        self._write_truth()
                        self.session.store.update_failure_injection(
                            self.session_id,
                            self._failure_ids[key],
                            {
                                "status": "injected",
                                "verify_result": verification,
                            },
                        )
                    self._record(
                        "verified",
                        action,
                        verification=verification,
                        root_causes=truth.model_dump(mode="json")["root_causes"],
                    )
                elif event.traffic == "background":
                    self._background_started = True
                    effective, workload = self._background
                    self.net_env.start_traffic_programs(workload)
                    self._record(
                        "applied", action, traffic_effective=effective.case_value()
                    )
                elif event.traffic == "business":
                    workload = self.net_env.start_business_workload()
                    self._record(
                        "applied", action, process_count=len(workload.processes)
                    )
                elif isinstance(event.traffic, Burst):
                    self._active[key] = None
                    self._bursts[key].start(
                        rate=event.traffic.rate,
                        packet_size=event.traffic.packet_size,
                        stop_at=self.t0 + action.at + event.duration,
                    )
                    self._record(
                        "applied",
                        action,
                        flows=[vars(flow) for flow in self._bursts[key].flows],
                        destination_ip=self._bursts[key].destination_ip,
                    )
                self._record("finished", action)
            self._notify()
        except BaseException as exc:
            if key in self._failure_ids:
                with self._journal_lock:
                    self.session.store.update_failure_injection(
                        self.session_id,
                        self._failure_ids[key],
                        {
                            "status": "recover_failed"
                            if action.stopping
                            else "inject_failed",
                            "error": str(exc),
                        },
                    )
            self._record("operation_failed", action, error=str(exc))
            self._fail(exc)
            raise

    def _stop(self, key: tuple[int, int]) -> dict[str, Any]:
        if key not in self._active:
            raise ScheduleError("Recovery ran before successful activation")
        if key in self._prepared:
            result = self._prepared[key].recover()
            self._prepared[key].problem.net_env.align_saved_configs()
        else:
            self._bursts[key].stop()
            result = {"verified": True}
        self._active.pop(key)
        self._recovered_at[key] = time.monotonic()
        if key in self._failure_ids:
            with self._journal_lock:
                self.session.store.update_failure_injection(
                    self.session_id,
                    self._failure_ids[key],
                    {
                        "status": "recovered",
                        "end_time": time.time(),
                        "recovery_result": result,
                    },
                )
        return result

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise ScheduleError(str(self._error)) from self._error
        if self.cancelled.is_set():
            raise ScheduleError("Session schedule was cancelled")

    def wait_agent_start(self) -> None:
        if self.cancelled.wait(
            max(0.0, self.t0 + (self.plan.agent_start or 0) - time.monotonic())
        ):
            self.raise_if_failed()
        self.raise_if_failed()
        self._agent_dispatch_ready.wait()
        self.raise_if_failed()
        if self.plan.agent_start is None:
            for future in self._initial_futures:
                try:
                    future.result()
                except Exception:
                    self.raise_if_failed()
                    raise
            if self.plan.static:
                self._thread.join()
            self.raise_if_failed()
        if self._initial_failure is not None:
            from nika.validation.presence import bind_injected_problem

            initial = self._initial_failure
            bind_injected_problem(self.session_id, initial.problem, initial.params)
            if not self.restored:
                from nika.validation.presence import (
                    fault_label,
                    record_injection_verify,
                )

                rows = self.session.store.list_failure_injections(
                    session_id=self.session_id
                )
                checks = [rows[i]["verify_result"] for i in self._failure_ids.values()]
                record_injection_verify(
                    fault=fault_label(initial.problem),
                    verify_result=checks[0]
                    if len(checks) == 1
                    else {
                        "verified": True,
                        "details": {"sub_results": checks},
                    },
                )
                validate_failure_effect(
                    self.session,
                    initial.problem,
                    list(self._failure_ids.values()),
                    [e.problem for e in self.plan.events],
                )
        self._record("agent_released", planned_offset=self.plan.agent_start)

    def observation_revision(self) -> int:
        with self._changed:
            return self._revision

    def wait_for_progress(self, revision: int, timeout: float | None = None) -> None:
        with self._changed:
            self._changed.wait_for(
                lambda: (
                    self._revision != revision
                    or self.complete.is_set()
                    or self.cancelled.is_set()
                ),
                timeout,
            )
        self.raise_if_failed()

    def close(self) -> None:
        """Stop dispatch, join owned work, and restore recoverable mutations."""
        with self._close_lock:
            if self._closed:
                return
            try:
                self._close()
            finally:
                self._closed = True

    def _close(self) -> None:
        self.cancelled.set()
        self._notify()
        if self._thread:
            self._thread.join()
        for action in self.plan.actions():
            if action not in self._dispatched:
                self._record("cancelled", action, reason="session closing")
        errors = []
        for key in reversed(list(self._active)):
            prepared = self._prepared.get(key)
            if prepared is not None and not callable(
                getattr(prepared.problem, "recover_fault", None)
            ):
                continue
            try:
                self._stop(key)
            except Exception as exc:
                errors.append(str(exc))
                # Preserve snapshots that depend on the failed recovery step.
                break
        for burst in self._bursts.values():
            try:
                burst.stop()
            except Exception as exc:
                errors.append(str(exc))
        if self._background_started:
            from nika.traffic.profiles import stop_background_traffic

            try:
                stop_background_traffic(self.net_env)
            except Exception as exc:
                errors.append(str(exc))
        with _RUNNERS_LOCK:
            _RUNNERS.pop(self.session_id, None)
        (self.directory / "schedule-owner.json").unlink(missing_ok=True)
        if errors and self._error is None:
            self._error = ScheduleError(f"Schedule cleanup failed: {errors}")
        self._record("closed", cleanup_errors=errors)
        if errors:
            raise ScheduleError(f"Schedule cleanup failed: {errors}")


def continuation_prompt(description: str, report: str) -> str:
    return (
        description
        + "\n\nObservation is continuing. Inspect the network again and update your diagnosis."
        + "\nRetain findings from earlier observations, including faults that have recovered."
        + "\nYour previous provisional report follows:\n"
        + report
    )
