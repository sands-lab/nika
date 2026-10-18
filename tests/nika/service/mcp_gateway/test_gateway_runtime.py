"""In-process MCP gateway behavior: worker threads, session binding, mounts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from nika.mcp.gateway.app import create_gateway_app
from nika.mcp.gateway.context import get_bound_session_id
from nika.mcp.gateway.session_registry import clear_sessions, register_session
from nika.mcp.servers.common.pingmesh_server import mcp as pingmesh_mcp

pytestmark = pytest.mark.integration

_ACCEPT = {"Accept": "application/json, text/event-stream"}
_URL = "/mcp/pingmesh_mcp_server/mcp"
_OPEN = {"tools": ["*"], "node_roles": ["*"], "node_ids": []}


def _probe_binding() -> str:
    """Report the bound session and whether an event loop runs this thread."""
    try:
        asyncio.get_running_loop()
        on_loop = True
    except RuntimeError:
        on_loop = False
    return json.dumps({"session": get_bound_session_id(), "on_loop": on_loop})


@pytest.fixture
def gateway() -> Iterator[TestClient]:
    clear_sessions()
    pingmesh_mcp.add_tool(_probe_binding, name="nika_test_probe_binding")
    try:
        with TestClient(
            create_gateway_app(), base_url="http://127.0.0.1:8000"
        ) as client:
            yield client
    finally:
        pingmesh_mcp.remove_tool("nika_test_probe_binding")
        clear_sessions()


def _rpc(payload: dict) -> dict:
    return {"jsonrpc": "2.0", **payload}


def _result(response) -> dict:
    text = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        data = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        ]
        return json.loads(data[-1])
    return response.json()


def _initialize(client: TestClient, session_id: str) -> str:
    response = client.post(
        _URL,
        headers={**_ACCEPT, "NIKA-Session-Id": session_id},
        json=_rpc(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        ),
    )
    assert response.status_code == 200, response.text
    transport_id = response.headers["mcp-session-id"]
    client.post(
        _URL,
        headers={
            **_ACCEPT,
            "NIKA-Session-Id": session_id,
            "mcp-session-id": transport_id,
        },
        json=_rpc({"method": "notifications/initialized"}),
    )
    return transport_id


def _call(client: TestClient, session_id: str, transport_id: str, name: str):
    return client.post(
        _URL,
        headers={
            **_ACCEPT,
            "NIKA-Session-Id": session_id,
            "mcp-session-id": transport_id,
        },
        json=_rpc(
            {"id": 2, "method": "tools/call", "params": {"name": name, "arguments": {}}}
        ),
    )


def test_sync_tool_runs_off_loop_with_session_binding(gateway: TestClient) -> None:
    register_session("sess-a", scenario_name="simple_bgp", access_policy=_OPEN)
    transport_id = _initialize(gateway, "sess-a")
    response = _call(gateway, "sess-a", transport_id, "nika_test_probe_binding")
    payload = json.loads(_result(response)["result"]["content"][0]["text"])
    assert payload == {"session": "sess-a", "on_loop": False}


def test_transport_rejects_a_different_session_header(gateway: TestClient) -> None:
    register_session("sess-a", scenario_name="simple_bgp", access_policy=_OPEN)
    register_session("sess-b", scenario_name="simple_bgp", access_policy=_OPEN)
    transport_id = _initialize(gateway, "sess-a")
    response = _call(gateway, "sess-b", transport_id, "nika_test_probe_binding")
    assert response.status_code == 403
    assert "initialized this MCP connection" in response.text


def test_unselected_diagnosis_server_is_denied(gateway: TestClient) -> None:
    register_session(
        "sess-a",
        scenario_name="simple_bgp",
        access_policy=_OPEN,
        diagnosis_servers=["kathara_base_mcp_server"],
    )
    transport_id = _initialize(gateway, "sess-a")
    response = _call(gateway, "sess-a", transport_id, "nika_test_probe_binding")
    body = _result(response)
    assert body["error"]["message"] == (
        "NIKA access denied: diagnosis_server_not_allowed"
    )
