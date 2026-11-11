"""Gateway startup failures clean up owned services and preserve live peers."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from nika.mcp.gateway import lifecycle as life
from nika.mcp.gateway.session_registry import (
    get_session,
    register_session,
    unregister_session,
)
from nika.utils.net import pick_free_port

pytestmark = pytest.mark.integration


def test_session_setup_failure_releases_gateway() -> None:
    before = set(life._live_managers)
    port = pick_free_port("127.0.0.1")
    try:
        with pytest.raises(FileNotFoundError):
            with life.mcp_gateway_for_session(
                "missing-gateway-startup-session", host="127.0.0.1", port=port
            ):
                pytest.fail("A missing session must not open a gateway lease")
        assert set(life._live_managers) == before
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    finally:
        for manager in set(life._live_managers) - before:
            life._shutdown_manager(manager)


def test_failed_sibling_preserves_an_initialized_mcp_connection() -> None:
    before = set(life._live_managers)
    manager = life.start_gateway(host="127.0.0.1", port=0)
    thread = manager._thread
    sid = "gateway-lifecycle-sibling"
    register_session(sid, access_policy={"tools": ["*"], "node_roles": ["*"]})
    try:

        async def exercise() -> None:
            headers = {
                "X-Session-Id": sid,
                "X-Session-Token": get_session(sid).access_token,
            }
            async with httpx.AsyncClient(headers=headers) as client:
                async with streamable_http_client(
                    manager.base_url + "/mcp/pingmesh_mcp_server/mcp",
                    http_client=client,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        with pytest.raises(FileNotFoundError):
                            with life.mcp_gateway_for_session(
                                "missing-gateway-startup-sibling",
                                host="127.0.0.1",
                                port=0,
                            ):
                                pytest.fail("Missing session accepted")
                        assert (await session.list_tools()).tools == tools.tools
                        assert manager in life._live_managers
                        assert life._active_manager is manager
                        assert os.environ[life.ENV_GATEWAY_URL] == manager.base_url
                        health = await client.get(manager.base_url + "/gateway/health")
                        assert health.json() == {"status": "ok"}

        asyncio.run(exercise())
    finally:
        unregister_session(sid)
        for owned in set(life._live_managers) - before:
            life._shutdown_manager(owned)
    assert thread is not None and not thread.is_alive()
    assert set(life._live_managers) == before


def test_occupied_listener_is_not_gateway_readiness() -> None:
    # Isolate uvicorn's bind-error SystemExit in its real server thread from
    # pytest's thread-exception hook. The owning listener remains untouched.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import socket
from nika.mcp.gateway import lifecycle as life
with socket.create_server(('127.0.0.1', 0)) as listener:
    port = listener.getsockname()[1]
    try:
        manager = life.start_gateway(host='127.0.0.1', port=port)
    except RuntimeError as exc:
        assert 'exited before startup' in str(exc), str(exc)
    else:
        life._shutdown_manager(manager)
        raise AssertionError('Occupied listener accepted as gateway readiness')
    assert not life._live_managers
    with socket.create_connection(listener.getsockname(), timeout=1):
        pass
""",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_gateway_shutdown_closes_an_open_mcp_event_stream() -> None:
    manager = life.start_gateway(host="127.0.0.1", port=0)
    thread = manager._thread
    sid = "gateway-lifecycle-open-stream"
    register_session(sid, access_policy={"tools": ["*"], "node_roles": ["*"]})
    try:
        headers = {
            "X-Session-Id": sid,
            "X-Session-Token": get_session(sid).access_token,
            "Accept": "application/json, text/event-stream",
        }
        with httpx.Client(headers=headers) as client:
            url = manager.base_url + "/mcp/pingmesh_mcp_server/mcp"
            initialized = client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "lifecycle-test", "version": "1"},
                    },
                },
            )
            initialized.raise_for_status()
            client.headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]
            client.post(
                url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
            ).raise_for_status()
            with client.stream("GET", url) as stream:
                assert stream.status_code == 200
                life._shutdown_manager(manager)
                assert thread is not None and not thread.is_alive()
                assert manager not in life._live_managers
            with socket.socket() as probe:
                assert probe.connect_ex((manager.host, manager.port)) != 0
    finally:
        unregister_session(sid)
        if manager in life._live_managers:
            life._shutdown_manager(manager)
