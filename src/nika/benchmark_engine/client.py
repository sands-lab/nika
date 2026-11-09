"""Frontend client of a benchmark engine container (contract B)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, TypeVar

from pydantic import BaseModel

from nika.benchmark_engine.protocol import (
    API_VERSION,
    ENGINE_TOKEN_HEADER,
    AgentEndpoints,
    AgentTask,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
    TrialHandle,
    TrialRequest,
    TrialState,
    TrialStatus,
)

_M = TypeVar("_M", bound=BaseModel)

# Ceiling for one request. The long operations answer at once, so only the
# short ones (gateway, fault check, scoring) come close to it.
_REQUEST_TIMEOUT_SEC = 600.0
# Status requests answer at once; a slow one counts as unreachable.
_STATUS_TIMEOUT_SEC = 30.0
# Deploying or preparing a large environment takes up to 30 minutes.
_SETTLE_TIMEOUT_SEC = 3600.0
# How long the engine may stay unreachable before a trial fails.
_UNREACHABLE_SEC = 60.0
_POLL_MIN_SEC = 0.05
_POLL_MAX_SEC = 2.0
# The states a trial passes through on its way to the agent.
_PROGRESS: tuple[TrialState, ...] = ("deploying", "deployed", "preparing", "ready")


class _Unreachable(EngineError):
    """The engine did not answer; repeating the request is safe."""


def _status_error(status: TrialStatus) -> Exception:
    message = f"benchmark engine trial {status.session_id} failed: {status.error}"
    if status.error_type == "FileNotFoundError":
        return FileNotFoundError(message)
    return EngineError(message, outcome=status.outcome or "infra_failed")


def _settled(status: TrialStatus, until: TrialState) -> bool:
    """True once *status* reached *until*; raises when it cannot get there."""
    if status.state == until:
        return True
    if status.state == "failed":
        raise _status_error(status)
    if until not in _PROGRESS:
        return False
    if status.state not in _PROGRESS:
        raise EngineError(f"benchmark engine trial {status.session_id} was stopped")
    return _PROGRESS.index(status.state) >= _PROGRESS.index(until)


class EngineClient:
    """Calls a benchmark engine over HTTP; implements ``LifecycleProtocol``.

    ``start_trial``, ``begin``, and ``stop_trial`` start the work on the
    engine and poll the trial's state until it settles, so no request stays
    open while an environment deploys and a dropped connection only costs a
    repeated request.
    """

    def __init__(self, base_url: str, *, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _call(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout: float = _REQUEST_TIMEOUT_SEC,
    ) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/v{API_VERSION}/{operation}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                ENGINE_TOKEN_HEADER: self.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}").get("result")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode() or "{}")
            except json.JSONDecodeError:
                body = {}
            message = f"benchmark engine {operation} failed: {body.get('error') or exc}"
            if body.get("error_type") == "FileNotFoundError":
                raise FileNotFoundError(message) from exc
            raise EngineError(
                message, outcome=body.get("outcome") or "infra_failed"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise _Unreachable(
                f"benchmark engine at {self.base_url} is unreachable: {exc}"
            ) from exc

    def _model(self, model: type[_M], operation: str, payload: dict[str, Any]) -> _M:
        return model.model_validate(self._call(operation, payload))

    def _status(self, operation: str, payload: dict[str, Any]) -> TrialStatus:
        """Send a trial request, repeating it while the engine is unreachable."""
        deadline = time.monotonic() + _UNREACHABLE_SEC
        delay = _POLL_MIN_SEC
        while True:
            try:
                raw = self._call(operation, payload, timeout=_STATUS_TIMEOUT_SEC)
                return TrialStatus.model_validate(raw)
            except _Unreachable:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SEC)

    def _await(
        self, operation: str, payload: dict[str, Any], until: TrialState
    ) -> TrialStatus:
        """Start a long operation, then poll the trial's state until it settles."""
        status = self._status(operation, payload)
        deadline = time.monotonic() + _SETTLE_TIMEOUT_SEC
        delay = _POLL_MIN_SEC
        while not _settled(status, until):
            if time.monotonic() >= deadline:
                raise EngineError(
                    f"benchmark engine trial {status.session_id} was not {until} "
                    f"after {_SETTLE_TIMEOUT_SEC:.0f}s (state: {status.state})"
                )
            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SEC)
            status = self._status("trial_status", {"session_id": status.session_id})
        return status

    def describe(self, release: str | None = None) -> EngineDescription:
        return self._model(EngineDescription, "describe", {"release": release})

    def start_trial(self, request: TrialRequest) -> TrialHandle:
        if request.session_id is None:
            from nika.utils.session_id import make_session_id

            # The frontend names the trial so it can ask about, and stop, a
            # deployment whose answer never reached it.
            request = request.model_copy(
                update={"session_id": make_session_id(session_tag=request.session_tag)}
            )
        try:
            status = self._await(
                "start_trial", {"request": request.model_dump(mode="json")}, "deployed"
            )
        except BaseException as exc:
            self._abandon(request, interrupted=not isinstance(exc, Exception))
            raise
        if status.handle is None:
            raise EngineError(
                f"benchmark engine trial {status.session_id} has no handle"
            )
        return status.handle

    def _abandon(self, request: TrialRequest, *, interrupted: bool) -> None:
        """Ask the engine to tear down a deployment this frontend gave up on.

        It does not wait: the engine stops the trial once its deployment ends.
        """
        if request.session_id is None or request.trial_dir is None:
            return
        handle = TrialHandle(session_id=request.session_id, trial_dir=request.trial_dir)
        try:
            self._call(
                "stop_trial",
                {
                    "handle": handle.model_dump(mode="json"),
                    "status": "aborted" if interrupted else "error",
                },
                timeout=_STATUS_TIMEOUT_SEC,
            )
        except Exception:  # noqa: BLE001 - the deployment's own error matters more
            pass

    def begin(self, handle: TrialHandle) -> AgentTask:
        status = self._await(
            "begin", {"handle": handle.model_dump(mode="json")}, "ready"
        )
        if status.task is None:
            raise EngineError(f"benchmark engine trial {status.session_id} has no task")
        return status.task

    def schedule_operation(self, handle: TrialHandle, operation: str) -> dict:
        return self._call(
            "schedule_operation",
            {"handle": handle.model_dump(mode="json"), "operation": operation},
        )

    def agent_endpoints(
        self, handle: TrialHandle, request: GatewayRequest
    ) -> AgentEndpoints:
        return self._model(
            AgentEndpoints,
            "agent_endpoints",
            {
                "handle": handle.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            },
        )

    def close_agent_endpoints(self, handle: TrialHandle) -> None:
        self._call("close_agent_endpoints", {"handle": handle.model_dump(mode="json")})

    def fault_check(self, handle: TrialHandle) -> FaultCheck:
        return self._model(
            FaultCheck, "fault_check", {"handle": handle.model_dump(mode="json")}
        )

    def score(
        self,
        handle: TrialHandle,
        *,
        result_dir: str | None = None,
        infra_evidence: bool = False,
    ) -> None:
        self._call(
            "score",
            {
                "handle": handle.model_dump(mode="json"),
                "result_dir": result_dir,
                "infra_evidence": infra_evidence,
            },
        )

    def stop_trial(self, handle: TrialHandle, *, status: str = "finished") -> None:
        # The agent sandbox runs on the frontend's host, outside the engine.
        _cleanup_sandbox(handle.session_id)
        self._await(
            "stop_trial",
            {"handle": handle.model_dump(mode="json"), "status": status},
            "stopped",
        )


def _cleanup_sandbox(session_id: str) -> None:
    """Remove the trial's agent sandbox if a hard-killed agent left it behind."""
    from nika.workflows.session.close import cleanup_session_sandbox
    from nika.utils.session_store import SessionStore

    try:
        row = SessionStore().get_session(session_id)
    except FileNotFoundError:
        return
    cleanup_session_sandbox(row, session_id=session_id)
