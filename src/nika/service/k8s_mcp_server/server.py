"""ASGI application for the in-node Kubernetes MCP server."""

from __future__ import annotations

import os
from contextlib import AsyncExitStack, asynccontextmanager

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from nika.service.k8s_mcp_server import DEFAULT_BIND, DEFAULT_PORT
from nika.service.k8s_mcp_server.client import get_client
from nika.service.k8s_mcp_server.tools import register_tools

SERVER_NAME = "k8s_mcp_server"


def build_mcp() -> FastMCP:
    mcp = FastMCP(SERVER_NAME)
    register_tools(mcp)
    return mcp


def create_app() -> Starlette:
    """Return Starlette app with ``/health`` and streamable MCP under ``/mcp``."""
    mcp = build_mcp()
    mcp_app = mcp.streamable_http_app()

    async def health(_request: Request) -> JSONResponse:
        try:
            nodes = get_client().list_nodes()
            return JSONResponse(
                {
                    "status": "ok",
                    "server": SERVER_NAME,
                    "ready_nodes": sum(1 for n in nodes if n.get("ready")),
                    "node_count": len(nodes),
                }
            )
        except Exception as exc:
            return JSONResponse(
                {"status": "error", "server": SERVER_NAME, "details": str(exc)},
                status_code=503,
            )

    async def root(_request: Request) -> PlainTextResponse:
        return PlainTextResponse(f"{SERVER_NAME} ok\n")

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(mcp.session_manager.run())
            yield

    return Starlette(
        routes=[
            Route("/", root),
            Route("/health", health),
            # FastMCP streamable_http_app already exposes ``/mcp``.
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def run(
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    import uvicorn

    bind = host or os.environ.get("NIKA_K8S_MCP_BIND", DEFAULT_BIND)
    listen_port = port
    if listen_port is None:
        listen_port = int(os.environ.get("NIKA_K8S_MCP_PORT", str(DEFAULT_PORT)))
    uvicorn.run(create_app(), host=bind, port=listen_port, log_level="warning")
