"""Agent-host observation of a timeline executed by the lab-host daemon."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from nika.remote.client import RemoteClient, RemoteError
from nika.remote.workflows import _sync_local_meta_from_remote, pull_session_artifacts
from nika.schedule import Schedule
from nika.utils.session import Session
from nika.workflows.schedule import (
    ScheduleError,
    _RUNNERS,
    _RUNNERS_LOCK,
    write_schedule_owner,
)


class RemoteSessionScheduler:
    """Keep agent continuation and cleanup on the existing remote session path."""

    def __init__(
        self,
        session_id: str,
        schedule: Schedule,
        *,
        traffic: Any = None,
        expected_root_causes: list | None = None,
        restored: bool = False,
        client: Any = None,
    ):
        self.session = Session().load_running_session(session_id=session_id)
        self.session_id = session_id
        self.directory = Path(self.session.session_dir)
        self.plan = schedule
        self.traffic = traffic
        self.expected_root_causes = expected_root_causes
        self.restored = restored
        self.client = client if client is not None else RemoteClient()
        # The agent pipeline only needs Event.is_set(), backed by lab-host status.
        self.complete = self
        self._closed = False
        self._prepared = False
        self._status: dict[str, Any] = {}
        self._polled_at = 0.0
        self._lock = threading.RLock()

    def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        try:
            return self.client.schedule_operation(self.session_id, operation, **kwargs)
        except RemoteError as exc:
            raise ScheduleError(str(exc)) from exc

    def _sync(self) -> None:
        _sync_local_meta_from_remote(
            self.session_id, self.client.get_session(self.session_id)
        )
        pull_session_artifacts(self.session_id, str(self.directory), client=self.client)

    def prepare(self) -> None:
        write_schedule_owner(self.directory, self.session_id)
        with _RUNNERS_LOCK:
            if self.session_id in _RUNNERS:
                raise ValueError("This session already has a scheduler")
            _RUNNERS[self.session_id] = self
        remote = self._call(
            "prepare",
            body={
                "schedule": self.plan.case_value(),
                "traffic": self.traffic,
                "benchmark_version": getattr(self.session, "benchmark_version", None),
                "expected_root_causes": self.expected_root_causes,
                "restored": self.restored,
            },
        )
        self._prepared = True
        _sync_local_meta_from_remote(self.session_id, remote)
        self._sync()

    def start(self) -> None:
        self._call("start")

    def wait_agent_start(self) -> None:
        self._call("agent-ready")
        if self.plan.agent_start is None:
            self._sync()

    def status(self) -> dict[str, Any]:
        return self._poll()

    def _poll(self) -> dict[str, Any]:
        with self._lock:
            if not self._closed and time.monotonic() - self._polled_at >= 0.1:
                status = self._call("status")
                if status.get("completed") and not self._status.get("completed"):
                    self._sync()
                self._status = status
                self._polled_at = time.monotonic()
            return dict(self._status)

    def is_set(self) -> bool:
        return bool(self._poll().get("completed"))

    def observation_revision(self) -> int:
        return int(self._poll().get("revision", 0))

    def raise_if_failed(self) -> None:
        status = self._poll()
        if status.get("error") or status.get("cancelled"):
            raise ScheduleError(status.get("error") or "Session schedule was cancelled")

    def wait_for_progress(self, revision: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.observation_revision() == revision and not self.is_set():
            self.raise_if_failed()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
        self.raise_if_failed()

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._prepared:
                self._status = self._call("close")
        finally:
            self._closed = True
            try:
                self._sync()
            finally:
                with _RUNNERS_LOCK:
                    _RUNNERS.pop(self.session_id, None)
                (self.directory / "schedule-owner.json").unlink(missing_ok=True)
