"""Live MCP gateway invocation for SDN Clos tools on sdn_l3_clos."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig
from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session
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
                        "sdn_get_fabric_state",
                        "sdn_controller_logs",
                        "sdn_endpoint_reachability",
                    }
                    assert not (required - set(tools)), sorted(required - set(tools))

                    return {
                        "fabric": _text(
                            await tools["sdn_get_fabric_state"].ainvoke(
                                {
                                    "switch_name": "leaf_1",
                                    "source": "client_1_1",
                                    "target_ip": "10.0.2.11",
                                    "log_rows": 20,
                                }
                            )
                        ),
                        "logs": _text(
                            await tools["sdn_controller_logs"].ainvoke({"rows": 20})
                        ),
                        "ping": _text(
                            await tools["sdn_endpoint_reachability"].ainvoke(
                                {
                                    "source": "client_1_1",
                                    "target_ip": "10.0.2.11",
                                    "count": 2,
                                }
                            )
                        ),
                    }

                results = asyncio.run(_run())

            fabric = json.loads(results["fabric"])
            assert "controller_programmed_intent" in fabric
            assert "switch_observed_state" in fabric
            assert "leaf_1" in fabric["switch_observed_state"]
            _assert_tool_ok(
                "sdn_get_fabric_state",
                results["fabric"],
                must_contain=("onos_topology", "group_id", "endpoint_reachability"),
            )
            _assert_tool_ok(
                "sdn_controller_logs", results["logs"], must_contain=("onos",)
            )
            _assert_tool_ok(
                "sdn_endpoint_reachability",
                results["ping"],
                must_contain=("ping_output", "10.0.2.11"),
            )
            assert "0% packet loss" in results["ping"] or "received" in results["ping"]
        finally:
            self._close_session(session_id)
