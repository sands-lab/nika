"""Starlette routes for the NIKA Remote daemon."""

from __future__ import annotations


from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from nika.remote.handlers import (
    handle_artifacts,
    handle_close_session,
    handle_env_start,
    handle_failure_inject,
    handle_fault_artifact,
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
        result = await run_in_threadpool(handle_env_start, body)
        return JSONResponse(result.model_dump())
    except Exception as exc:  # noqa: BLE001 - map to HTTP error
        return _error_response(exc, status=400)


async def failure_inject(request: Request) -> JSONResponse:
    try:
        body = FailureInjectRequest.model_validate(await request.json())
        result = await run_in_threadpool(handle_failure_inject, body)
        return JSONResponse(result.model_dump())
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def fault_artifact(request: Request) -> JSONResponse:
    try:
        result = await run_in_threadpool(
            handle_fault_artifact, request.path_params["session_id"]
        )
        return JSONResponse(result)
    except FileNotFoundError as exc:
        return _error_response(exc, status=404)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def mcp_attach(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        raw = await request.json()
        body = McpAttachRequest.model_validate(raw or {})
        result = await run_in_threadpool(handle_mcp_attach, session_id, body)
        return JSONResponse(result.model_dump())
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def mcp_detach(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        await run_in_threadpool(handle_mcp_detach, session_id)
        return JSONResponse({"ok": True, "session_id": session_id})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_close(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        raw = await request.json()
        body = SessionCloseRequest.model_validate(raw or {})
        await run_in_threadpool(
            handle_close_session, session_id, undeploy=body.undeploy, stop_all=False
        )
        return JSONResponse({"ok": True, "session_id": session_id})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def sessions_wipe(request: Request) -> JSONResponse:
    try:
        raw = await request.json()
        body = SessionCloseRequest.model_validate(raw or {})
        await run_in_threadpool(
            handle_close_session, None, undeploy=body.undeploy, stop_all=True
        )
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
        sessions = await run_in_threadpool(
            handle_list_sessions, running_only=running_only
        )
        return JSONResponse({"sessions": sessions})
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_get(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        return JSONResponse(await run_in_threadpool(handle_get_session, session_id))
    except FileNotFoundError as exc:
        return _error_response(exc, status=404)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_containers(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    try:
        result = await run_in_threadpool(handle_session_containers, session_id)
        return JSONResponse(result.model_dump())
    except FileNotFoundError as exc:
        return _error_response(exc, status=404)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc, status=400)


async def session_artifacts(request: Request) -> Response:
    session_id = request.path_params["session_id"]
    try:
        payload = await run_in_threadpool(handle_artifacts, session_id)
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


def create_remote_app() -> Starlette:
    """Build the remote daemon ASGI application (no shared-token auth)."""
    routes = [
        Route("/health", health, methods=["GET"]),
        Route("/v1/env/start", env_start, methods=["POST"]),
        Route("/v1/failure/inject", failure_inject, methods=["POST"]),
        Route(
            "/v1/sessions/{session_id}/fault-artifact",
            fault_artifact,
            methods=["GET"],
        ),
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
    return Starlette(routes=routes)
