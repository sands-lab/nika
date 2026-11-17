"""Build the NIKA MCP HTTP gateway ASGI application."""

from __future__ import annotations

import hmac
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from importlib import import_module

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from agent.protocols import PHASES, SUBMISSION
from nika.runtime.extras import raise_missing_extra
from nika.service.mcp_gateway.middleware import (
    PhaseGateMiddleware,
    _empty_mcp,
)
from nika.service.mcp_gateway.remote_proxy import RemoteMcpProxy
from nika.service.mcp_gateway.session_registry import advance_phase, get_session
from nika.service.mcp_server.registry import MCP_SERVER_SPECS

_MCP_MODULE_ATTRS: dict[str, tuple[str, str]] = {
    "kathara_base_mcp_server": (
        "nika.service.mcp_server.common.host_server",
        "mcp",
    ),
    "pingmesh_mcp_server": (
        "nika.service.mcp_server.common.pingmesh_server",
        "mcp",
    ),
    "task_mcp_server": ("nika.service.mcp_server.common.task_server", "mcp"),
    "kathara_frr_mcp_server": (
        "nika.service.mcp_server.kathara.frr_server",
        "mcp",
    ),
    "kathara_bmv2_mcp_server": (
        "nika.service.mcp_server.kathara.bmv2_server",
        "mcp",
    ),
    "kathara_telemetry_mcp_server": (
        "nika.service.mcp_server.kathara.telemetry_server",
        "mcp",
    ),
    "containerlab_srl_mcp_server": (
        "nika.service.mcp_server.containerlab.srl_server",
        "mcp",
    ),
    "k8s_mcp_server": ("nika.service.k8s_mcp_server.server", "mcp"),
}


def _load_mcp(name: str) -> FastMCP:
    module_path, attr = _MCP_MODULE_ATTRS[name]
    module = import_module(module_path)
    mcp: FastMCP = getattr(module, attr)
    return mcp


def _iter_mountable_mcp_names(*, backend: str | None = None):
    """Yield MCP server names for *backend* (common + matching backend; remotes always)."""
    for name, spec in MCP_SERVER_SPECS.items():
        if spec.remote:
            yield name
            continue
        if name not in _MCP_MODULE_ATTRS:
            continue
        if spec.backend is None:
            yield name
            continue
        if backend is not None and spec.backend == backend:
            yield name


def reset_gateway_mcp_state(*, backend: str | None = None) -> None:
    """Allow a fresh gateway process to attach new HTTP session managers."""
    for name in _iter_mountable_mcp_names(backend=backend):
        spec = MCP_SERVER_SPECS[name]
        if spec.remote:
            continue
        try:
            _load_mcp(name)._session_manager = None  # type: ignore[attr-defined]
        except ImportError:
            continue
    _empty_mcp._session_manager = None  # type: ignore[attr-defined]


async def gateway_health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


PHASE_TOKEN_HEADER = "NIKA-Phase-Token"


async def gateway_advance_phase(request: Request) -> JSONResponse:
    from nika.service.mcp_gateway.middleware import SESSION_HEADER, request_session_id

    session_id = request.path_params["session_id"]
    header_sid = request_session_id(
        {name.lower(): value for name, value in request.scope.get("headers", [])}
    )
    if header_sid != session_id:
        return JSONResponse(
            {"error": f"{SESSION_HEADER} must match path session_id"},
            status_code=403,
        )
    entry = get_session(session_id)
    if entry is None:
        return JSONResponse({"error": "session not registered"}, status_code=404)
    if entry.phase_token is not None and not hmac.compare_digest(
        request.headers.get(PHASE_TOKEN_HEADER, "").strip(), entry.phase_token
    ):
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

    if entry.phase_token is None:
        try:
            advance_phase(session_id, phase)  # type: ignore[arg-type]
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, "phase": phase})
    return await _advance_engine_trial(entry, phase, body)


async def _advance_engine_trial(entry, phase: str, body: dict) -> JSONResponse:
    """Phase advance on a benchmark engine's trial gateway.

    The frontend, not the agent, advances the phase. Entering submission
    freezes the diagnosis report and returns the submission context: the
    release's fault types and resources, as its task tools list them.
    """
    from nika.service.mcp_gateway.tool_log import (
        freeze_diagnosis,
        frozen_diagnosis_report,
    )

    session_dir = entry.session_dir or ""
    report: str | None = None
    if phase == SUBMISSION:
        given = body.get("diagnosis_report")
        if isinstance(given, str):
            report = freeze_diagnosis(session_dir, given)
        else:
            report = frozen_diagnosis_report(session_dir)
            if report is None:
                return JSONResponse(
                    {
                        "error": (
                            "diagnosis_report is required to advance to "
                            "submission when diagnosis is not yet frozen"
                        )
                    },
                    status_code=400,
                )
    try:
        advance_phase(entry.session_id, phase)  # type: ignore[arg-type]
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    if phase != SUBMISSION:
        return JSONResponse({"ok": True, "phase": phase})

    fault_ontology, resources = await run_in_threadpool(
        _submission_catalogs, entry.session_id
    )
    for listed in (fault_ontology, resources):
        if isinstance(listed, dict) and listed.get("error"):
            return JSONResponse(
                {"error": f"could not list the submission context: {listed}"},
                status_code=500,
            )
    return JSONResponse(
        {
            "ok": True,
            "phase": phase,
            "submission_context": {
                "diagnosis_report": report,
                "fault_ontology": fault_ontology,
                "resources": resources,
            },
        }
    )


def _submission_catalogs(session_id: str) -> tuple[object, object]:
    """``list_avail_problems()`` and ``list_resources()`` for *session_id*."""
    from nika.service.mcp_gateway.context import bind_session, reset_session
    from nika.service.mcp_server.common import task_server

    token = bind_session(session_id)
    try:
        return task_server.list_avail_problems(), task_server.list_resources()
    finally:
        reset_session(token)


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

    When *backend* is ``None``, only common (backend-neutral) servers and remote
    proxies are mounted — never default to Kathara.
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
        if spec.remote:
            # After Mount(/mcp/{name}), remaining path is ``/mcp`` (client URL
            # ends with ``/mcp/{name}/mcp``). Forward that path to the in-node
            # server which also serves streamable HTTP under ``/mcp``.
            inner = PhaseGateMiddleware(
                RemoteMcpProxy(name),
                server_name=name,
                blocked_app=blocked_app,
            )
            routes.append(Mount(f"/mcp/{name}", app=inner))
            continue

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
