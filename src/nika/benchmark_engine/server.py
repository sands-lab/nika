"""HTTP service of a benchmark engine container (contract B).

It serves the lifecycle operations under ``/v<API version>/<operation>``. Only
the frontend calls it: every request carries the run's engine token. The
agent reaches the MCP gateway that ``agent_endpoints`` opens for its trial,
with that trial's own tokens.
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
    EngineError,
    GatewayRequest,
    TrialHandle,
    TrialRequest,
)


logger = logging.getLogger("nika.benchmark_engine")

_Operation = Callable[[BenchmarkEngine, dict[str, Any]], BaseModel | None]

OPERATIONS: dict[str, _Operation] = {
    "describe": lambda e, a: e.describe(a.get("release")),
    "start_trial": lambda e, a: e.start_trial(TrialRequest(**a["request"])),
    "inject": lambda e, a: e.inject(
        TrialHandle(**a["handle"]), TrialRequest(**a["request"])
    ),
    "agent_endpoints": lambda e, a: e.agent_endpoints(
        TrialHandle(**a["handle"]), GatewayRequest(**a["request"])
    ),
    "close_agent_endpoints": lambda e, a: e.close_agent_endpoints(
        TrialHandle(**a["handle"])
    ),
    "fault_check": lambda e, a: e.fault_check(TrialHandle(**a["handle"])),
    "score": lambda e, a: e.score(
        TrialHandle(**a["handle"]),
        result_dir=a.get("result_dir"),
        infra_evidence=bool(a.get("infra_evidence")),
    ),
    "stop_trial": lambda e, a: e.stop_trial(
        TrialHandle(**a["handle"]), status=str(a.get("status") or "finished")
    ),
}


def _authorized(headers: dict[bytes, bytes], token: str) -> bool:
    presented = headers.get(ENGINE_TOKEN_HEADER.lower().encode(), b"").decode()
    return hmac.compare_digest(presented, token)


def create_engine_app(engine: BenchmarkEngine, *, token: str) -> Starlette:
    """Return the engine's ASGI app: the lifecycle operations."""

    async def operation(request: Request) -> JSONResponse:
        if not _authorized(dict(request.scope.get("headers") or []), token):
            return JSONResponse({"error": "invalid engine token"}, status_code=401)
        name = request.path_params["operation"]
        handler = OPERATIONS.get(name)
        if handler is None:
            return JSONResponse(
                {"error": f"unknown operation {name!r}"}, status_code=404
            )
        try:
            args = await request.json()
            result = await run_in_threadpool(handler, engine, args)
        except (ValidationError, KeyError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - reported to the frontend
            logger.exception("engine operation %s failed", name)
            return JSONResponse(
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "outcome": (
                        exc.outcome if isinstance(exc, EngineError) else "infra_failed"
                    ),
                },
                status_code=500,
            )
        return JSONResponse(
            {"result": result.model_dump(mode="json") if result is not None else None}
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
