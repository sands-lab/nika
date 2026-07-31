"""Starlette routes for the NIKA Remote daemon."""

from __future__ import annotations

import json
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nika.remote.handlers import (
    handle_artifacts,
    handle_close_session,
    handle_env_start,
    handle_failure_inject,
    handle_get_session,
    handle_list_sessions,
    handle_mcp_attach,
    handle_mcp_detach,
    handle_session_containers,
)
from nika.remote.protocol import (
    EnvStartRequest,
    ErrorBody,
    FailureInjectRequest,
    HealthResponse,
    McpAttachRequest,
    SessionCloseRequest,
)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Optional shared-token auth for the remote daemon."""

    def __init__(self, app: Any, *, token: str) -> None:
        super().__init__(app)
        self.token = token.strip()

    async def dispatch(self, request: Request, call_next: Callable):
        if not self.token:
            return await call_next(request)
        if request.url.path == "/health":
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if auth != expected:
            return JSONResponse(
                ErrorBody(error="Unauthorized", error_type="AuthError").model_dump(),
                status_code=401,
            )
        return await call_next(request)


def _error_response(exc: BaseException, *, status: int = 400) -> JSONResponse:
    return JSONResponse(
        ErrorBody(error=str(exc), error_type=type(exc).__name__).model_dump(),
        status_code=status,
    )


async def health(_request: Request) -> JSONResponse:
    return JSONResponse(HealthResponse().model_dump())


async def env_start(request: Request) -> JSONResponse:
    try:
        body = EnvStartRequest.model_validate(await request.json())
        result = handle_env_start(body)
        return JSONResponse(result.model_dump())
    except Exception as exc:  # noqa: BLE001 - map to HTTP error
        return _error_response(exc, status=400)


async def failure_inject(request: Request) -> JSONResponse:
    try:
        body = FailureInjectRequest.model_validate(await request.json())
        result = handle_failure_inject(body)
        return JSONResponse(result.model_dump())
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def mcp_attach(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        raw = await request.json()
        body = McpAttachRequest.model_validate(raw or {})
        result = handle_mcp_attach(session_id, body)
        return JSONResponse(result.model_dump())
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def mcp_detach(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        handle_mcp_detach(session_id)
        return JSONResponse({"ok": True, "session_id": session_id})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_close(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        raw = await request.json()
        body = SessionCloseRequest.model_validate(raw or {})
        handle_close_session(session_id, undeploy=body.undeploy, stop_all=False)
        return JSONResponse({"ok": True, "session_id": session_id})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def sessions_wipe(request: Request) -> JSONResponse:
    try:
        raw = await request.json()
        body = SessionCloseRequest.model_validate(raw or {})
        handle_close_session(None, undeploy=body.undeploy, stop_all=True)
        return JSONResponse({"ok": True, "wiped": True})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def sessions_list(request: Request) -> JSONResponse:
    try:
        running_only = request.query_params.get("running_only", "true").lower() in {
            "1",
            "true",
            "yes",
        }
        sessions = handle_list_sessions(running_only=running_only)
        return JSONResponse({"sessions": sessions})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_get(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        return JSONResponse(handle_get_session(session_id))
    except FileNotFoundError as exc:
        return _error_response(exc, status=404)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_containers(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        result = handle_session_containers(session_id)
        return JSONResponse(result.model_dump())
    except FileNotFoundError as exc:
        return _error_response(exc, status=404)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_artifacts(request: Request) -> Response:
    session_id = request.path_params["session_id"]
    try:
        payload = handle_artifacts(session_id)
        return Response(
            payload,
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{session_id}.tar.gz"'
            },
        )
    except FileNotFoundError as exc:
        return _error_response(exc, status=404)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


def create_remote_app(*, token: str = "") -> Starlette:
    """Build the remote daemon ASGI application."""
    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/v1/env/start", env_start, methods=["POST"]),
        Route("/v1/failure/inject", failure_inject, methods=["POST"]),
        Route("/v1/sessions", sessions_list, methods=["GET"]),
        Route("/v1/sessions/wipe", sessions_wipe, methods=["POST"]),
        Route("/v1/sessions/{session_id}", session_get, methods=["GET"]),
        Route("/v1/sessions/{session_id}/close", session_close, methods=["POST"]),
        Route(
            "/v1/sessions/{session_id}/containers",
            session_containers,
            methods=["GET"],
        ),
        Route("/v1/sessions/{session_id}/mcp/attach", mcp_attach, methods=["POST"]),
        Route("/v1/sessions/{session_id}/mcp/detach", mcp_detach, methods=["POST"]),
        Route(
            "/v1/sessions/{session_id}/artifacts",
            session_artifacts,
            methods=["GET"],
        ),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def dumps_error(message: str) -> str:
    return json.dumps({"error": message})
