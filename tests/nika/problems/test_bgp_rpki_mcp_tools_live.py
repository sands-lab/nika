"""Live MCP gateway invocation for BGP/RPKI tools on Abilene eBGP."""

from __future__ import annotations

import asyncio
import time

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig
from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.inject_targets import isp_inject_params
from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available


def _text(raw: object) -> str:
    texts = tool_text_list(raw)
    return "\n".join(texts).strip()


def _assert_tool_ok(
    name: str, text: str, *, must_contain: tuple[str, ...] = ()
) -> None:
    assert text, f"{name} returned empty output"
    lowered = text.lower()
    for bad in (
        "traceback",
        "tool error",
        "error executing",
        "exception:",
        "iserror",
        "failed to",
    ):
        assert bad not in lowered, f"{name} looks like an error:\n{text[:800]}"
    for needle in must_contain:
        assert needle.lower() in lowered, f"{name} missing {needle!r} in:\n{text[:800]}"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestBGPRPKIMCPToolsLive(IntegrationTestCase):
    """Call new tools through the real MCP gateway (not FRR API shortcuts)."""

    def test_mcp_bgp_rpki_tools_return_usable_output(self) -> None:
        isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
        bgp = compile_bgp_plan(isp_plan, "ebgp", rpki=True)
        assert bgp is not None
        inv = bgp.inventory
        leaker = str(inv["leaker_device"])
        rov = str(inv["rov_observer"])
        non_rov = str(inv["non_rov_observer"])
        leaker_asn = str(inv["leaker_asn"])
        params = isp_inject_params(
            "bgp_rpki_invalid_route_leak", isp_plan.inventory, inv
        )

        session_id = self._start_env(
            "isp",
            [
                "--topo",
                "abilene",
                "--igp",
                "ospf",
                "--bgp-mode",
                "ebgp",
                "--rpki",
            ],
        )
        try:
            self._assert_session_ready(session_id, "isp")
            time.sleep(25)
            self._inject_failure(
                "bgp_rpki_invalid_route_leak",
                params,
                session_id=session_id,
            )
            time.sleep(10)

            with mcp_gateway_for_session(session_id, scenario_name="isp"):
                config = MCPServerConfig(session_id=session_id).load_http_config(
                    [
                        "kathara_frr_mcp_server",
                        "kathara_base_mcp_server",
                    ]
                )

                async def _run() -> dict[str, str]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {tool.name: tool for tool in await client.get_tools()}
                    required = {
                        "frr_get_routing_state",
                        "frr_get_rpki_status",
                        "traceroute",
                    }
                    missing = required - set(tools)
                    assert not missing, f"MCP tools missing: {sorted(missing)}"

                    out: dict[str, str] = {}
                    out["frr_get_routing_state_summary"] = _text(
                        await tools["frr_get_routing_state"].ainvoke({"device": leaker})
                    )
                    out["frr_get_routing_state_routes"] = _text(
                        await tools["frr_get_routing_state"].ainvoke(
                            {
                                "device": non_rov,
                                "prefix": "203.0.113.0/24",
                            }
                        )
                    )
                    out["frr_get_routing_state_neighbors"] = _text(
                        await tools["frr_get_routing_state"].ainvoke({"device": leaker})
                    )
                    out["frr_get_rpki_status"] = _text(
                        await tools["frr_get_rpki_status"].ainvoke(
                            {
                                "device": rov,
                                "prefix": "203.0.113.0/24",
                            }
                        )
                    )
                    out["traceroute"] = _text(
                        await tools["traceroute"].ainvoke(
                            {"host_name": non_rov, "dst_ip": "203.0.113.1"}
                        )
                    )
                    return out

                results = asyncio.run(_run())

            _assert_tool_ok(
                "frr_get_routing_state_summary",
                results["frr_get_routing_state_summary"],
                must_contain=("bgp", "as"),
            )
            _assert_tool_ok(
                "frr_get_routing_state_routes",
                results["frr_get_routing_state_routes"],
                must_contain=("203.0.113", leaker_asn),
            )
            _assert_tool_ok(
                "frr_get_routing_state_neighbors",
                results["frr_get_routing_state_neighbors"],
                must_contain=("bgp", "neighbor"),
            )
            _assert_tool_ok(
                "frr_get_rpki_status",
                results["frr_get_rpki_status"],
                must_contain=("rpki",),
            )
            rpki = results["frr_get_rpki_status"].lower()
            assert (
                "connected" in rpki
                or "65001" in rpki
                or "prefix" in rpki
                or "cache" in rpki
            ), results["frr_get_rpki_status"][:800]
            _assert_tool_ok("traceroute", results["traceroute"])
            assert (
                "traceroute" in results["traceroute"].lower()
                or "hop" in results["traceroute"].lower()
                or "*" in results["traceroute"]
                or "ms" in results["traceroute"].lower()
            ), results["traceroute"][:800]
        finally:
            self._close_session(session_id)
