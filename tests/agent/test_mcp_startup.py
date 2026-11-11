"""Declared MCP capabilities must be connected before a diagnosis is accepted."""

import pytest

from agent.utils.mcp_client import mcp_startup_error

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("status", ["failed", "pending", "disabled", None])
def test_required_mcp_server_cannot_be_unavailable(status):
    servers = [{"name": "network", "status": status}]
    error = mcp_startup_error(servers, {"network"})
    assert error is not None and f"network={status}" in error


def test_missing_required_server_is_not_a_successful_empty_inventory():
    assert mcp_startup_error([], {"network"}) == (
        "Required MCP servers are not connected: network=missing"
    )


def test_only_declared_servers_must_be_connected():
    assert (
        mcp_startup_error(
            [
                {"name": "network", "status": "connected"},
                {"name": "unrequested", "status": "failed"},
            ],
            {"network"},
        )
        is None
    )
    assert mcp_startup_error([], set()) is None
