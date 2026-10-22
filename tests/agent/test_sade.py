from __future__ import annotations
import pytest
import sys
from agent.sdk.mcp import to_sdk_mcp_servers

pytestmark = pytest.mark.unit


class SadeMcpAdapterTest:
    """MCP config adaptation for claude-agent-sdk."""

    def test_converts_transport_to_stdio_type(self) -> None:
        servers = to_sdk_mcp_servers(
            {
                "kathara_base_mcp_server": {
                    "transport": "stdio",
                    "command": "python3",
                    "args": ["/path/base.py"],
                    "env": {"SESSION": "sess-abc"},
                }
            }
        )
        srv = servers["kathara_base_mcp_server"]
        assert srv["type"] == "stdio"
        assert srv["command"] == sys.executable
        assert srv["args"] == ["/path/base.py"]
        assert srv["env"]["SESSION"] == "sess-abc"

    def test_multiple_servers_all_present(self) -> None:
        servers = to_sdk_mcp_servers(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/base.py"],
                },
                "task_mcp_server": {"command": "python3", "args": ["/path/task.py"]},
            }
        )
        assert "kathara_base_mcp_server" in servers
        assert "task_mcp_server" in servers
