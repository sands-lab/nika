"""Docker-backed proof that gateway authorization survives phase changes."""

from __future__ import annotations

import asyncio

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.shared.exceptions import McpError

from nika.agents.mcp_config import (
    MCPServerConfig,
    begin_submission_mcp_phase,
    engine_gateway,
)
from nika.benchmark_engine.protocol import AccessPolicy, AgentAccess, GatewayRequest
from nika.mcp.registry import (
    DIAGNOSIS_HOST_SERVER,
    SUBMISSION_SERVER,
)
from nika.utils.session_store import SessionStore
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(not docker_available(), reason="Requires Docker/Kathara")
class McpAccessE2ETest(IntegrationTestCase):
    def test_network_admin_cannot_access_hosts_or_diagnose_after_submission(
        self,
    ) -> None:
        session_id = self._start_env("simple_bgp")
        session_dir = SessionStore().get_session(session_id)["session_dir"]
        # A release's access policy reaches the engine's gateway through the
        # protocol, not through the run config.
        access = AgentAccess(
            role="network-admin",
            policy=AccessPolicy(tools=["exec_shell", "submit"], node_roles=["router"]),
        )
        try:
            with engine_gateway(session_id, session_dir, GatewayRequest(access=access)):

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
            self._close_session(session_id)
