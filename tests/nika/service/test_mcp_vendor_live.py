"""Real gateway calls through vendor-specific CLI access paths."""

from __future__ import annotations

import asyncio

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import (
    docker_available,
    docker_image_available,
    min3clos_prerequisites,
)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestVendorMcpLive(IntegrationTestCase):
    @pytest.mark.parametrize(
        ("scenario", "server", "tool", "node", "command", "extra_args"),
        [
            pytest.param(
                "iosxr_simple_bgp",
                "kathara_iosxr_mcp_server",
                "iosxr_exec",
                "router1",
                "show version",
                None,
                marks=pytest.mark.skipif(
                    not docker_image_available("ios-xr/xrd-control-plane:26.2.1"),
                    reason="IOS-XR image not available",
                ),
                id="iosxr",
            ),
            pytest.param(
                "routeros_simple_bgp",
                "kathara_routeros_mcp_server",
                "routeros_exec",
                "router1",
                "/system resource print",
                None,
                marks=pytest.mark.skipif(
                    not docker_image_available("vrnetlab/mikrotik_routeros:7.21.5"),
                    reason="RouterOS image not available",
                ),
                id="routeros",
            ),
            pytest.param(
                "min3clos",
                "containerlab_srl_mcp_server",
                "srl_exec_cli",
                "leaf1",
                "show version",
                None,
                marks=pytest.mark.skipif(
                    not min3clos_prerequisites(),
                    reason="Containerlab, gnmic, or Docker not available",
                ),
                id="srlinux",
            ),
        ],
    )
    def test_vendor_cli_returns_live_state(
        self,
        scenario: str,
        server: str,
        tool: str,
        node: str,
        command: str,
        extra_args: list[str] | None,
    ) -> None:
        session_id = self._start_env(scenario, extra_args)
        try:
            self._assert_session_ready(session_id, scenario)
            with mcp_gateway_for_session(session_id, scenario_name=scenario):
                cfg = MCPServerConfig(session_id).load_http_config(
                    ["kathara_base_mcp_server", server]
                )

                async def _run() -> tuple[str, str]:
                    client = MultiServerMCPClient(connections=cfg)
                    tools = {t.name: t for t in await client.get_tools()}
                    assert tool in tools and "exec_shell" in tools
                    cli_output = await tools[tool].ainvoke(
                        {
                            "router_name" if node == "router1" else "device_name": node,
                            "command": command,
                        }
                    )
                    host_output = await tools["exec_shell"].ainvoke(
                        {"host_name": node, "command": "hostname"}
                    )
                    return (
                        "\n".join(tool_text_list(cli_output)),
                        "\n".join(tool_text_list(host_output)),
                    )

                cli, host = asyncio.run(_run())
            assert node in host
            assert cli and "version" in cli.lower()
            assert "tool_execution_error" not in cli.lower()
        finally:
            self._close_session(session_id)
