"""Live MCP gateway invocation for SDN Clos tools on sdn_l3_clos."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available


def _text(raw: object) -> str:
    return "\n".join(tool_text_list(raw)).strip()


def _assert_tool_ok(
    name: str, text: str, *, must_contain: tuple[str, ...] = ()
) -> None:
    assert text, f"{name} returned empty output"
    lowered = text.lower()
    for bad in (
        "traceback",
        "tool error",
        "tool_execution_error",
        "exception:",
    ):
        assert bad not in lowered, f"{name} looks like an error:\n{text[:800]}"
    for needle in must_contain:
        assert needle.lower() in lowered, f"{name} missing {needle!r} in:\n{text[:800]}"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestSDNMCPToolsLive(IntegrationTestCase):
    """Call SDN tools through the real MCP gateway on a live Clos lab."""

    def test_mcp_sdn_tools_return_usable_output(self) -> None:
        session_id = self._start_env("sdn_l3_clos", ["-s", "s"])
        try:
            self._assert_session_ready(session_id, "sdn_l3_clos")

            with mcp_gateway_for_session(session_id, scenario_name="sdn_l3_clos"):
                config = MCPServerConfig(session_id=session_id).load_http_config(
                    ["kathara_sdn_mcp_server"]
                )

                async def _run() -> dict[str, str]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {tool.name: tool for tool in await client.get_tools()}
                    required = {
                        "sdn_onos_rest",
                        "sdn_ovs_exec",
                        "sdn_controller_logs",
                    }
                    assert not (required - set(tools)), sorted(required - set(tools))

                    return {
                        "devices": _text(
                            await tools["sdn_onos_rest"].ainvoke(
                                {"path": "/onos/v1/devices"}
                            )
                        ),
                        "flows": _text(
                            await tools["sdn_ovs_exec"].ainvoke(
                                {
                                    "switch_name": "leaf_1",
                                    "command": (
                                        "ovs-ofctl -O OpenFlow13 dump-flows leaf_1"
                                    ),
                                }
                            )
                        ),
                        "logs": _text(
                            await tools["sdn_controller_logs"].ainvoke({"rows": 20})
                        ),
                    }

                results = asyncio.run(_run())

            devices = json.loads(results["devices"])
            assert devices["path"] == "/onos/v1/devices"
            assert "body" in devices
            flows = json.loads(results["flows"])
            assert flows["switch"] == "leaf_1"
            assert "dump-flows" in flows["command"]
            _assert_tool_ok(
                "sdn_onos_rest",
                results["devices"],
                must_contain=("onos_oob", "body"),
            )
            _assert_tool_ok(
                "sdn_ovs_exec",
                results["flows"],
                must_contain=("leaf_1", "output"),
            )
            _assert_tool_ok(
                "sdn_controller_logs", results["logs"], must_contain=("onos",)
            )
        finally:
            self._close_session(session_id)
