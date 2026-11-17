"""Run each trial in its own process, as release 0.1.0's runner did.

Release 0.1.0 ran every benchmark trial in a spawned process with its own MCP
gateway, because Kathara clients, the event log, and the gateway's servers
are process-global. The engine service keeps that isolation: each trial gets a
worker process that runs :class:`BenchmarkEngine` for that trial alone, from
``start_trial`` to ``stop_trial``, and hosts the trial's gateway.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from typing import Any

from nika.benchmark_engine.protocol import (
    AgentEndpoints,
    AgentTask,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
    Scores,
    TaskList,
    TrialHandle,
    TrialRequest,
)

logger = logging.getLogger("nika.benchmark_engine")

_STOP_JOIN_SEC = 30.0


def _worker_main(conn) -> None:
    """Serve one trial's lifecycle calls until the parent closes the pipe."""
    from nika.benchmark_engine.engine import BenchmarkEngine

    engine = BenchmarkEngine()
    while True:
        try:
            message = conn.recv()
        except EOFError:
            return
        if message is None:
            return
        operation, args, kwargs = message
        try:
            result = getattr(engine, operation)(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - returned to the parent
            outcome = getattr(exc, "outcome", "infra_failed")
            conn.send((False, (type(exc).__name__, str(exc), outcome)))
        else:
            conn.send((True, result))


def _raise(error_type: str, message: str, outcome: str) -> None:
    if error_type == "FileNotFoundError":
        raise FileNotFoundError(message)
    raise EngineError(message, outcome=outcome)  # type: ignore[arg-type]


class _Worker:
    """One trial's process; calls into it run one at a time."""

    def __init__(self, session_id: str) -> None:
        context = multiprocessing.get_context("spawn")
        self._conn, child = context.Pipe()
        self.process = context.Process(
            target=_worker_main, args=(child,), name=f"trial-{session_id}"
        )
        self.process.start()
        child.close()
        self._lock = threading.Lock()

    def call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            try:
                self._conn.send((operation, args, kwargs))
                ok, value = self._conn.recv()
            except (EOFError, OSError) as exc:
                raise EngineError(
                    f"trial worker exited during {operation} "
                    f"(exit code {self.process.exitcode}): {exc}"
                ) from exc
        if not ok:
            _raise(*value)
        return value

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.send(None)
            except (OSError, ValueError):
                pass
            self._conn.close()
        self.process.join(_STOP_JOIN_SEC)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(5.0)


class ProcessPerTrialEngine:
    """Lifecycle operations with each trial in its own worker process.

    ``describe``, ``list_tasks``, and ``score`` only read release files and
    trial artifacts, so they run in the service process.
    """

    def __init__(self) -> None:
        from nika.benchmark_engine.engine import BenchmarkEngine

        self._local = BenchmarkEngine()
        self._workers: dict[str, _Worker] = {}
        self._lock = threading.Lock()

    def _worker(self, session_id: str, *, create: bool = False) -> _Worker:
        with self._lock:
            worker = self._workers.get(session_id)
            if worker is not None and not worker.process.is_alive():
                self._workers.pop(session_id, None)
                worker = None
            if worker is None:
                if not create:
                    raise EngineError(f"trial {session_id} is not running here")
                worker = _Worker(session_id)
                self._workers[session_id] = worker
            return worker

    def _retire(self, session_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(session_id, None)
        if worker is not None:
            worker.close()

    def describe(self, release: str | None = None) -> EngineDescription:
        return self._local.describe(release)

    def list_tasks(self, release: str, split: str) -> TaskList:
        return self._local.list_tasks(release, split)

    def start_trial(self, request: TrialRequest) -> TrialHandle:
        if not request.session_id:
            raise EngineError("start_trial needs the session_id the frontend chose")
        # A new attempt of the same session starts in a fresh process.
        self._retire(request.session_id)
        worker = self._worker(request.session_id, create=True)
        try:
            return worker.call("start_trial", request)
        except BaseException:
            self._retire(request.session_id)
            raise

    def begin(self, handle: TrialHandle) -> AgentTask:
        return self._worker(handle.session_id).call("begin", handle)

    def agent_endpoints(
        self, handle: TrialHandle, request: GatewayRequest
    ) -> AgentEndpoints:
        return self._worker(handle.session_id).call("agent_endpoints", handle, request)

    def close_agent_endpoints(self, handle: TrialHandle) -> None:
        try:
            worker = self._worker(handle.session_id)
        except EngineError:
            return
        worker.call("close_agent_endpoints", handle)

    def fault_check(self, handle: TrialHandle) -> FaultCheck:
        return self._worker(handle.session_id).call("fault_check", handle)

    def score(self, handle: TrialHandle, *, infra_evidence: bool = False) -> Scores:
        return self._local.score(handle, infra_evidence=infra_evidence)

    def stop_trial(self, handle: TrialHandle, *, status: str = "finished") -> None:
        """Close the trial in its process, or in a new one after a restart."""
        worker = self._worker(handle.session_id, create=True)
        try:
            worker.call("stop_trial", handle, status=status)
        finally:
            self._retire(handle.session_id)

    def stop_all(self) -> None:
        """End every worker; used when the service shuts down."""
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.close()
