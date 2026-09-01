"""Docker live smoke: invoke MCP tools through the real session gateway."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.utils.mcp_servers import MCPServerConfig
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available, docker_image_available


def _text(raw: object) -> str:
    return "\n".join(tool_text_list(raw)).strip()


def _assert_ok(name: str, text: str) -> None:
    assert text, f"{name} returned empty output"
    lowered = text.lower()
    for bad in ("traceback", "tool_execution_error", "nika_session_id is not set"):
        assert bad not in lowered, f"{name} looks like an error:\n{text[:800]}"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestHostFrrPingmeshCaptureMcpLive(IntegrationTestCase):
    """simple_bgp: host + FRR + pingmesh + packet_capture via MCP gateway."""

    def test_core_diagnosis_tools_return_usable_output(self) -> None:
        session_id = self._start_env("simple_bgp")
        try:
            self._assert_session_ready(session_id, "simple_bgp")
            servers = [
                "kathara_base_mcp_server",
                "kathara_frr_mcp_server",
                "pingmesh_mcp_server",
                "packet_capture_mcp_server",
            ]
            with mcp_gateway_for_session(session_id, scenario_name="simple_bgp"):
                config = MCPServerConfig(session_id=session_id).load_http_config(
                    servers
                )

                async def _run() -> dict[str, str]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    required = {
                        "ping_pair",
                        "traceroute",
                        "get_host_net_config",
                        "exec_shell",
                        "netstat",
                        "frr_exec",
                        "frr_show_ip_route",
                        "frr_show_running_config",
                        "frr_get_bgp_conf",
                        "run_pingmesh_snapshot",
                        "packet_capture_start",
                        "packet_capture_stop",
                        "packet_capture_inspect",
                    }
                    missing = required - set(tools)
                    assert not missing, sorted(missing)
                    assert "get_reachability" not in tools
                    assert "frr_get_routing_state" not in tools

                    out: dict[str, str] = {}
                    out["ping_pair"] = _text(
                        await tools["ping_pair"].ainvoke(
                            {"host_a": "pc1", "host_b": "pc2", "count": 2}
                        )
                    )
                    out["traceroute"] = _text(
                        await tools["traceroute"].ainvoke(
                            {"host_name": "pc1", "dst_ip": "195.11.14.1"}
                        )
                    )
                    out["get_host_net_config"] = _text(
                        await tools["get_host_net_config"].ainvoke(
                            {"host_name": "router1"}
                        )
                    )
                    out["exec_shell"] = _text(
                        await tools["exec_shell"].ainvoke(
                            {"host_name": "router1", "command": "hostname"}
                        )
                    )
                    out["netstat"] = _text(
                        await tools["netstat"].ainvoke({"host_name": "router1"})
                    )
                    out["frr_exec"] = _text(
                        await tools["frr_exec"].ainvoke(
                            {
                                "router_name": "router1",
                                "command": "show ip bgp summary",
                            }
                        )
                    )
                    out["frr_show_ip_route"] = _text(
                        await tools["frr_show_ip_route"].ainvoke(
                            {"router_name": "router1"}
                        )
                    )
                    out["frr_show_running_config"] = _text(
                        await tools["frr_show_running_config"].ainvoke(
                            {"router_name": "router1"}
                        )
                    )
                    out["frr_get_bgp_conf"] = _text(
                        await tools["frr_get_bgp_conf"].ainvoke(
                            {"router_name": "router1"}
                        )
                    )
                    out["run_pingmesh_snapshot"] = _text(
                        await tools["run_pingmesh_snapshot"].ainvoke({})
                    )

                    start = _text(
                        await tools["packet_capture_start"].ainvoke(
                            {
                                "device": "pc1",
                                "interface": "eth0",
                                "capture_filter": "icmp",
                                "max_duration_sec": 5,
                                "max_packets": 20,
                            }
                        )
                    )
                    start_payload = json.loads(start)
                    capture_id = start_payload.get("capture_id")
                    assert capture_id, start
                    await tools["ping_pair"].ainvoke(
                        {"host_a": "pc1", "host_b": "pc2", "count": 2}
                    )
                    out["packet_capture_stop"] = _text(
                        await tools["packet_capture_stop"].ainvoke(
                            {"capture_id": capture_id}
                        )
                    )
                    out["packet_capture_inspect"] = _text(
                        await tools["packet_capture_inspect"].ainvoke(
                            {
                                "capture_id": capture_id,
                                "view": "summary",
                                "limit": 5,
                            }
                        )
                    )
                    out["packet_capture_start"] = start
                    return out

                results = asyncio.run(_run())

            for name, text in results.items():
                _assert_ok(name, text)
            assert "router1" in results["exec_shell"]
            mesh = json.loads(results["run_pingmesh_snapshot"])
            assert "results" in mesh or "endpoints" in mesh
        finally:
            self._close_session(session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
@pytest.mark.skipif(
    not docker_image_available("kathara/p4"),
    reason="kathara/p4 image not available",
)
class TestP4McpLive(IntegrationTestCase):
    """p4_dc_fabric: p4rt_exec via MCP gateway."""

    def test_p4rt_exec_returns_live_state(self) -> None:
        session_id = self._start_env("p4_dc_fabric", ["-s", "s"])
        try:
            self._assert_session_ready(session_id, "p4_dc_fabric")
            with mcp_gateway_for_session(session_id, scenario_name="p4_dc_fabric"):
                config = MCPServerConfig(session_id=session_id).load_http_config(
                    ["kathara_bmv2_mcp_server"]
                )

                async def _run() -> str:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    assert set(tools) == {"p4rt_exec"}
                    return _text(
                        await tools["p4rt_exec"].ainvoke(
                            {"args": "read --switch leaf_1"}
                        )
                    )

                raw = asyncio.run(_run())

            _assert_ok("p4rt_exec", raw)
            payload = json.loads(raw)
            assert "internal_fault" not in raw
            assert "switches" in payload or payload.get("ok") is True
        finally:
            self._close_session(session_id)
