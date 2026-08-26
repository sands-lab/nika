"""Docker-backed proof that gateway authorization survives phase changes."""

from __future__ import annotations

import asyncio

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.shared.exceptions import McpError

from agent.utils.mcp_client import begin_submission_mcp_phase
from agent.utils.mcp_servers import MCPServerConfig
from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig
from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session
from nika.service.mcp_server.registry import (
    DIAGNOSIS_HOST_SERVER,
    SUBMISSION_SERVER,
)
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


@pytest.mark.skipif(not docker_available(), reason="Requires Docker/Kathara")
class McpAccessE2ETest(IntegrationTestCase):
    def test_network_admin_cannot_access_hosts_or_diagnose_after_submission(
        self,
    ) -> None:
        session_id = self._start_env("simple_bgp")
        config = RunConfig.model_validate(
            {
                "agent": {
                    "access": {
                        "role": "network-admin",
                        "roles": {
                            "network-admin": {
                                "tools": ["exec_shell", "submit"],
                                "node_roles": ["router"],
                            }
                        },
                    }
                }
            }
        )
        set_run_config(config)
        try:
            with mcp_gateway_for_session(session_id, scenario_name="simple_bgp"):

                async def exercise() -> tuple[str, str, str]:
                    client = MultiServerMCPClient(
                        connections=MCPServerConfig(session_id).load_http_config(
                            [DIAGNOSIS_HOST_SERVER, SUBMISSION_SERVER]
                        )
                    )
                    tools = {tool.name: tool for tool in await client.get_tools()}
                    allowed = await tools["exec_shell"].ainvoke(
                        {"host_name": "router1", "command": "hostname"}
                    )
                    with pytest.raises(McpError, match="node_not_allowed"):
                        await tools["exec_shell"].ainvoke(
                            {"host_name": "pc1", "command": "hostname"}
                        )
                    begin_submission_mcp_phase(session_id, "router evidence frozen")
                    with pytest.raises(
                        McpError, match="submission_network_access_denied"
                    ):
                        await tools["exec_shell"].ainvoke(
                            {"host_name": "router1", "command": "hostname"}
                        )
                    return (
                        str(allowed),
                        "node_not_allowed",
                        "submission_network_access_denied",
                    )

                allowed, denied, after_switch = asyncio.run(exercise())
            assert "router1" in allowed
            assert denied == "node_not_allowed"
            assert after_switch == "submission_network_access_denied"
        finally:
            reset_run_config()
            self._close_session(session_id)
