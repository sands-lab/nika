"""HTTP service of a benchmark engine container (contract B).

It serves the lifecycle operations under ``/v<API version>/<operation>``. Only
the frontend calls it: every request carries the run's engine token. The
agent reaches the MCP gateway that ``agent_endpoints`` opens for its trial,
with that trial's own tokens.

``start_trial``, ``begin``, and ``stop_trial`` take minutes, so they answer at
once with the trial's state and run in the background; the frontend polls
``trial_status``. No request stays open while an environment deploys.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import uvicorn
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nika.benchmark_engine.engine import BenchmarkEngine
from nika.benchmark_engine.protocol import (
    API_VERSION,
    ENGINE_TOKEN_HEADER,
    AgentTask,
    EngineError,
    FailureOutcome,
    GatewayRequest,
    LifecycleProtocol,
    TrialHandle,
    TrialRequest,
    TrialState,
    TrialStatus,
)


logger = logging.getLogger("nika.benchmark_engine")

_Operation = Callable[[dict[str, Any]], BaseModel | None]

# States from which ``start_trial`` starts a new attempt, such as a retry.
_RESTARTABLE: frozenset[TrialState] = frozenset({"stopped", "failed"})


def _outcome(error: BaseException) -> FailureOutcome:
    return error.outcome if isinstance(error, EngineError) else "infra_failed"


class _Trial:
    """One trial's state; its engine work runs one operation at a time."""

    def __init__(self, session_id: str, state: TrialState) -> None:
        self.session_id = session_id
        self.state: TrialState = state
        self.handle: TrialHandle | None = None
        self.task: AgentTask | None = None
        self.error: BaseException | None = None
        self.work = threading.Lock()

    def status(self) -> TrialStatus:
        error = self.error
        return TrialStatus(
            session_id=self.session_id,
            state=self.state,
            handle=self.handle,
            task=self.task,
            error=str(error) if error is not None else None,
            error_type=type(error).__name__ if error is not None else None,
            outcome=_outcome(error) if error is not None else None,
        )


class _Trials:
    """Runs the long lifecycle operations in the background, per trial.

    Repeating ``start_trial``, ``begin``, or ``stop_trial`` returns the trial's
    current state instead of redoing the work, so the frontend can resend one
    after a dropped connection. A ``stop_trial`` sent while the trial deploys
    or prepares waits for that work, then tears the environment down.
    """

    def __init__(self, engine: LifecycleProtocol) -> None:
        self._engine = engine
        self._trials: dict[str, _Trial] = {}
        self._lock = threading.Lock()

    def start(self, args: dict[str, Any]) -> TrialStatus:
        request = TrialRequest(**args["request"])
        session_id = request.session_id
        if not session_id:
            raise EngineError("start_trial needs the session_id the frontend chose")
        with self._lock:
            trial = self._trials.get(session_id)
            if trial is None or trial.state in _RESTARTABLE:
                trial = _Trial(session_id, "deploying")
                self._trials[session_id] = trial
                self._run(
                    trial,
                    "start_trial",
                    lambda: self._engine.start_trial(request),
                    done="deployed",
                )
            return trial.status()

    def begin(self, args: dict[str, Any]) -> TrialStatus:
        handle = TrialHandle(**args["handle"])
        with self._lock:
            trial = self._get(handle.session_id)
            if trial.state == "deploying":
                raise EngineError(f"trial {trial.session_id} is still deploying")
            if trial.state == "deployed":
                trial.state = "preparing"
                self._run(
                    trial,
                    "begin",
                    lambda: self._engine.begin(trial.handle or handle),
                    done="ready",
                )
            return trial.status()

    def stop(self, args: dict[str, Any]) -> TrialStatus:
        handle = TrialHandle(**args["handle"])
        status = str(args.get("status") or "finished")
        with self._lock:
            trial = self._trials.get(handle.session_id)
            if trial is None:
                # Not started through this service, e.g. before a restart.
                trial = _Trial(handle.session_id, "stopping")
                self._trials[handle.session_id] = trial
            elif trial.state in ("stopping", "stopped"):
                return trial.status()
            trial.state = "stopping"
            trial.error = None
            self._run(
                trial,
                "stop_trial",
                lambda: self._engine.stop_trial(trial.handle or handle, status=status),
                done="stopped",
            )
            return trial.status()

    def status(self, args: dict[str, Any]) -> TrialStatus:
        with self._lock:
            return self._get(str(args["session_id"])).status()

    def _get(self, session_id: str) -> _Trial:
        trial = self._trials.get(session_id)
        if trial is None:
            raise FileNotFoundError(f"Trial {session_id!r} not found")
        return trial

    def _run(
        self,
        trial: _Trial,
        operation: str,
        work: Callable[[], Any],
        *,
        done: TrialState,
    ) -> None:
        def target() -> None:
            with trial.work:
                result: Any = None
                error: BaseException | None = None
                try:
                    result = work()
                except BaseException as exc:  # noqa: BLE001 - reported in the status
                    logger.exception(
                        "engine operation %s failed for trial %s",
                        operation,
                        trial.session_id,
                    )
                    error = exc
                with self._lock:
                    if isinstance(result, TrialHandle):
                        trial.handle = result
                    elif isinstance(result, AgentTask):
                        trial.task = result
                    if trial.state == "stopping" and done != "stopped":
                        return  # A queued stop_trial decides the final state.
                    trial.state = done if error is None else "failed"
                    trial.error = error

        threading.Thread(
            target=target, name=f"{operation}-{trial.session_id}", daemon=True
        ).start()


def engine_operations(engine: LifecycleProtocol) -> dict[str, _Operation]:
    """The lifecycle operations *engine* serves, by name."""
    trials = _Trials(engine)
    return {
        "describe": lambda a: engine.describe(a.get("release")),
        "start_trial": trials.start,
        "begin": trials.begin,
        "trial_status": trials.status,
        "agent_endpoints": lambda a: engine.agent_endpoints(
            TrialHandle(**a["handle"]), GatewayRequest(**a["request"])
        ),
        "close_agent_endpoints": lambda a: engine.close_agent_endpoints(
            TrialHandle(**a["handle"])
        ),
        "schedule_operation": lambda a: engine.schedule_operation(
            TrialHandle(**a["handle"]), a["operation"]
        ),
        "fault_check": lambda a: engine.fault_check(TrialHandle(**a["handle"])),
        "score": lambda a: engine.score(
            TrialHandle(**a["handle"]),
            result_dir=a.get("result_dir"),
            infra_evidence=bool(a.get("infra_evidence")),
        ),
        "stop_trial": trials.stop,
    }


def _authorized(headers: dict[bytes, bytes], token: str) -> bool:
    presented = headers.get(ENGINE_TOKEN_HEADER.lower().encode(), b"").decode()
    return hmac.compare_digest(presented, token)


def create_engine_app(engine: LifecycleProtocol, *, token: str) -> Starlette:
    """Return the engine's ASGI app: the lifecycle operations."""
    operations = engine_operations(engine)

    async def operation(request: Request) -> JSONResponse:
        if not _authorized(dict(request.scope.get("headers") or []), token):
            return JSONResponse({"error": "invalid engine token"}, status_code=401)
        name = request.path_params["operation"]
        handler = operations.get(name)
        if handler is None:
            return JSONResponse(
                {"error": f"unknown operation {name!r}"}, status_code=404
            )
        try:
            args = await request.json()
            result = await run_in_threadpool(handler, args)
        except (ValidationError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - reported to the frontend
            logger.exception("engine operation %s failed", name)
            return JSONResponse(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "outcome": _outcome(exc),
                },
                status_code=500,
            )
        return JSONResponse(
            {
                "result": result.model_dump(mode="json")
                if isinstance(result, BaseModel)
                else result
            }
        )

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "api": API_VERSION})

    return Starlette(
        routes=[
            Route("/health", health),
            Route(f"/v{API_VERSION}/{{operation}}", operation, methods=["POST"]),
        ]
    )


def _exit_with_parent(pid: int, interval_sec: float = 5.0) -> None:
    """End this process once the frontend process *pid* is gone."""

    def watch() -> None:
        while True:
            time.sleep(interval_sec)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                logger.warning("frontend process %s exited; stopping", pid)
                os._exit(0)
            except PermissionError:
                pass

    threading.Thread(target=watch, name="frontend-watch", daemon=True).start()


def serve_engine(
    *, host: str, port: int, token: str, parent_pid: int | None = None
) -> None:
    """Serve this NIKA's benchmark engine over HTTP (blocking)."""
    from nika.remote.server import configure_host_logging

    from nika.benchmark_engine import ENV_ENGINE_SERVER

    os.environ[ENV_ENGINE_SERVER] = "1"
    configure_host_logging()
    logging.getLogger("nika.benchmark_engine").addHandler(logging.StreamHandler())
    if parent_pid:
        _exit_with_parent(parent_pid)
    app = create_engine_app(BenchmarkEngine(), token=token)
    logger.warning("benchmark engine listening on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
