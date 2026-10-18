"""Build the NIKA MCP HTTP gateway ASGI application."""

from __future__ import annotations

import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from importlib import import_module

import anyio

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from nika.utils.dependencies import raise_missing_extra
from agent.protocols import PHASES, SUBMISSION
from nika.mcp.gateway.middleware import (
    SESSION_HEADER,
    PhaseGateMiddleware,
    _empty_mcp,
)
from nika.mcp.gateway.phase import phase_advance_token_matches
from nika.mcp.gateway.session_registry import advance_phase, get_session
from nika.mcp.registry import MCP_SERVER_SPECS

# Host-only secret (see ``phase_advance_token``). The session id alone is known
# to the agent, so it cannot authorize a jump to the submission context.
PHASE_TOKEN_HEADER = "NIKA-Phase-Token"


def _offload_sync_tools(mcp: FastMCP) -> None:
    """Run every synchronous tool in a worker thread.

    FastMCP calls non-async tools inline on the event loop, and one uvicorn
    loop serves every mounted server, so one slow ``docker exec`` would stall
    all sessions.  ``anyio.to_thread.run_sync`` copies the caller's context, so
    the gateway's session binding stays visible to the tool.
    """
    for tool in mcp._tool_manager.list_tools():  # noqa: SLF001 - FastMCP API gap
        if tool.is_async:
            continue
        sync_fn = tool.fn

        async def run_in_thread(*, _sync_fn=sync_fn, **kwargs):
            return await anyio.to_thread.run_sync(partial(_sync_fn, **kwargs))

        tool.fn = run_in_thread
        tool.is_async = True


def _load_mcp(name: str) -> FastMCP:
    mcp: FastMCP = import_module(MCP_SERVER_SPECS[name].module).mcp
    _offload_sync_tools(mcp)
    return mcp


def _iter_mountable_mcp_names(*, backend: str | None = None):
    """Yield MCP server names for *backend* (common + matching backend)."""
    for name, spec in MCP_SERVER_SPECS.items():
        if spec.backend is None or (backend is not None and spec.backend == backend):
            yield name


def reset_gateway_mcp_state(*, backend: str | None = None) -> None:
    """Allow a fresh gateway process to attach new HTTP session managers."""
    for name in _iter_mountable_mcp_names(backend=backend):
        try:
            _load_mcp(name)._session_manager = None  # type: ignore[attr-defined]
        except ImportError:
            continue
    _empty_mcp._session_manager = None  # type: ignore[attr-defined]


async def gateway_health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def gateway_advance_phase(request: Request) -> JSONResponse:
    session_id = request.path_params["session_id"]
    header_sid = request.headers.get(SESSION_HEADER, "").strip()
    if header_sid != session_id:
        return JSONResponse(
            {"error": f"{SESSION_HEADER} must match path session_id"},
            status_code=403,
        )
    session = get_session(session_id)
    if session is None:
        return JSONResponse({"error": "session not registered"}, status_code=404)
    token = request.headers.get(PHASE_TOKEN_HEADER, "").strip()
    if not phase_advance_token_matches(session_id, token):
        return JSONResponse(
            {"error": f"{PHASE_TOKEN_HEADER} is missing or invalid"},
            status_code=403,
        )

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    phase = body.get("phase")
    if phase not in PHASES:
        return JSONResponse(
            {"error": f"phase must be one of {PHASES!r}"},
            status_code=400,
        )

    # Sandbox agents cannot write the host trajectory; freeze on the gateway
    # when entering submission so submit() can load the immutable report.
    if phase == SUBMISSION:
        from nika.workflows.agent.submission import (
            freeze_diagnosis,
            load_frozen_diagnosis_report,
        )

        report = body.get("diagnosis_report")
        if isinstance(report, str) and report:
            freeze_diagnosis(session_id, report)
        elif load_frozen_diagnosis_report(session_id) is None:
            return JSONResponse(
                {
                    "error": (
                        "diagnosis_report is required to advance to submission "
                        "when diagnosis is not yet frozen"
                    )
                },
                status_code=400,
            )

    try:
        advance_phase(session_id, phase)  # type: ignore[arg-type]
    except KeyError:
        return JSONResponse({"error": "session not registered"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    from nika.workflows.agent.submission import load_submission_context

    context = load_submission_context(session.canonical_session_id)
    return JSONResponse({"ok": True, "phase": phase, "submission_context": context})


def _should_relax_host_checks() -> bool:
    """Allow non-localhost Host headers (NIKA Remote / cross-host MCP clients)."""
    from nika.remote.config import ENV_REMOTE_SERVER

    return os.environ.get(ENV_REMOTE_SERVER, "").strip() in {"1", "true", "yes", "on"}


def _apply_transport_security(mcp: FastMCP, *, relax_host_checks: bool) -> None:
    if not relax_host_checks:
        return
    # FastMCP defaults host=127.0.0.1 which auto-enables DNS-rebinding protection
    # limited to localhost. Remote agents send Host: <lab-ip>:<port> and get 421.
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )


def create_gateway_app(*, backend: str | None = None) -> Starlette:
    """Return a Starlette app exposing MCP servers for *backend* over HTTP.

    When *backend* is ``None``, only common (backend-neutral) servers are
    mounted; never default to Kathara.  FastMCP server objects are process
    global, so callers that may build gateways concurrently must serialize
    this call (see ``McpGatewayManager.start``).
    """
    reset_gateway_mcp_state(backend=backend)
    relax_host_checks = _should_relax_host_checks()
    routes: list = [
        Route("/gateway/health", gateway_health),
        Route(
            "/gateway/sessions/{session_id}/phase",
            gateway_advance_phase,
            methods=["POST"],
        ),
    ]
    session_managers = []

    _apply_transport_security(_empty_mcp, relax_host_checks=relax_host_checks)
    blocked_app = _empty_mcp.streamable_http_app()
    session_managers.append(_empty_mcp.session_manager)

    for name in _iter_mountable_mcp_names(backend=backend):
        spec = MCP_SERVER_SPECS[name]
        try:
            mcp = _load_mcp(name)
        except ImportError as exc:
            if spec.backend is None:
                raise
            raise_missing_extra(spec.backend, cause=exc)
        _apply_transport_security(mcp, relax_host_checks=relax_host_checks)
        starlette_app = mcp.streamable_http_app()
        session_managers.append(mcp.session_manager)
        inner = PhaseGateMiddleware(
            starlette_app,
            server_name=name,
            blocked_app=blocked_app,
        )
        routes.append(Mount(f"/mcp/{name}", app=inner))

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            for manager in session_managers:
                await stack.enter_async_context(manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)
