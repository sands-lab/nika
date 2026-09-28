"""Docker live smoke: invoke MCP tools through the real session gateway."""

from __future__ import annotations

import asyncio
import json
import re

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
class TestHostPingmeshCaptureMcpLive(IntegrationTestCase):
    """simple_bgp: generic execution, pingmesh, and capture via MCP gateway."""

    def test_core_diagnosis_tools_return_live_evidence(self) -> None:
        session_id = self._start_env("simple_bgp")
        try:
            self._assert_session_ready(session_id, "simple_bgp")
            servers = [
                "kathara_base_mcp_server",
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
                    assert set(tools) == {
                        "exec_shell",
                        "curl_web_test",
                        "iperf_test",
                        "active_tcp_probe",
                        "run_pingmesh_snapshot",
                        "packet_capture_start",
                        "packet_capture_stop",
                        "packet_capture_inspect",
                    }

                    out: dict[str, str] = {}
                    out["hostname"] = _text(
                        await tools["exec_shell"].ainvoke(
                            {"host_name": "router1", "command": "hostname"}
                        )
                    )
                    out["route"] = _text(
                        await tools["exec_shell"].ainvoke(
                            {
                                "host_name": "router1",
                                "command": "vtysh -c 'show ip route'",
                            }
                        )
                    )
                    out["config"] = _text(
                        await tools["exec_shell"].ainvoke(
                            {
                                "host_name": "router1",
                                "command": "vtysh -c 'show running-config'",
                            }
                        )
                    )
                    out["mesh"] = _text(
                        await tools["run_pingmesh_snapshot"].ainvoke({})
                    )
                    start = _text(
                        await tools["packet_capture_start"].ainvoke(
                            {
                                "device": "pc1",
                                "interface": "eth0",
                                "capture_filter": "icmp",
                                "max_duration_sec": 10,
                                "max_packets": 20,
                            }
                        )
                    )
                    capture_id = json.loads(start)["capture_id"]
                    try:
                        await asyncio.sleep(0.3)
                        out["ping"] = _text(
                            await tools["exec_shell"].ainvoke(
                                {"host_name": "pc1", "command": "ping -c 2 195.11.14.1"}
                            )
                        )
                    finally:
                        out["capture_stop"] = _text(
                            await tools["packet_capture_stop"].ainvoke(
                                {"capture_id": capture_id}
                            )
                        )
                    out["capture_inspect"] = _text(
                        await tools["packet_capture_inspect"].ainvoke(
                            {"capture_id": capture_id, "view": "summary", "limit": 5}
                        )
                    )
                    server_pid = _text(
                        await tools["exec_shell"].ainvoke(
                            {
                                "host_name": "pc1",
                                "command": "python3 -m http.server 18080 --bind 127.0.0.1 >/tmp/nika-mcp-http.log 2>&1 </dev/null & echo $!",
                            }
                        )
                    ).strip()
                    assert server_pid.isdigit(), server_pid
                    try:
                        await asyncio.sleep(0.2)
                        out["curl"] = _text(
                            await tools["curl_web_test"].ainvoke(
                                {
                                    "host_name": "pc1",
                                    "url": "http://127.0.0.1:18080/",
                                    "times": 2,
                                }
                            )
                        )
                        out["http_status"] = _text(
                            await tools["exec_shell"].ainvoke(
                                {
                                    "host_name": "pc1",
                                    "command": "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/",
                                }
                            )
                        )
                    finally:
                        await tools["exec_shell"].ainvoke(
                            {
                                "host_name": "pc1",
                                "command": f"kill {server_pid} 2>/dev/null || true",
                            }
                        )
                    out["iperf"] = _text(
                        await tools["iperf_test"].ainvoke(
                            {
                                "client_host_name": "pc1",
                                "server_host_name": "pc2",
                                "duration": 1,
                            }
                        )
                    )
                    out["active_probe"] = _text(
                        await tools["active_tcp_probe"].ainvoke(
                            {
                                "source": "pc1",
                                "destination": "pc2",
                                "source_port": 42600,
                                "destination_port": 42601,
                                "payload_seed": 1,
                                "payload_size": 64,
                                "packets": 2,
                            }
                        )
                    )
                    return out

                results = asyncio.run(_run())

            for name, output in results.items():
                _assert_ok(name, output)
            assert "router1" in results["hostname"]
            assert "0% packet loss" in results["ping"]
            assert "Routing entry" in results["route"] or "Codes:" in results["route"]
            assert "router bgp" in results["config"]
            mesh = json.loads(results["mesh"])
            assert "results" in mesh or "endpoints" in mesh
            assert json.loads(results["capture_stop"])["capture_id"]
            assert json.loads(results["capture_inspect"])["total_available"] >= 1
            assert results["curl"].count("namelookup:") == 2
            assert results["http_status"] == "200"
            assert "receiver" in results["iperf"].lower()
            assert re.search(r"acked['\"]?\s*:\s*2", results["active_probe"])
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


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
@pytest.mark.skipif(
    not docker_image_available("kathara/p4"),
    reason="kathara/p4 image not available",
)
class TestTelemetryMcpLive(IntegrationTestCase):
    """p4_dc_gateway: query packet traces produced by live fabric traffic."""

    def test_int_query_telemetry_returns_observed_hops(self) -> None:
        from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model

        model = build_gateway_fabric_model("s")
        session_id = self._start_env("p4_dc_gateway", ["-s", "s"])
        try:
            self._assert_session_ready(session_id, "p4_dc_gateway")
            with mcp_gateway_for_session(session_id, scenario_name="p4_dc_gateway"):
                config = MCPServerConfig(session_id=session_id).load_http_config(
                    ["kathara_telemetry_mcp_server", "kathara_base_mcp_server"]
                )

                async def _run() -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    assert "int_query_telemetry" in tools and "exec_shell" in tools
                    probe = await tools["exec_shell"].ainvoke(
                        {
                            "host_name": model.clients[0].name,
                            "command": f"nc -z -w 1 {model.services[0].ip} 80 || true",
                        }
                    )
                    assert "tool_execution_error" not in _text(probe).lower()
                    for _ in range(5):
                        result = await tools["int_query_telemetry"].ainvoke(
                            {"start_time": "0", "limit": 100}
                        )
                        if isinstance(result, str):
                            rows = json.loads(result)
                            if rows:
                                return rows
                        if (
                            isinstance(result, list)
                            and result
                            and isinstance(result[0], dict)
                            and "packet_timestamp" in result[0]
                        ):
                            return result
                        rows = [
                            json.loads(text) for text in tool_text_list(result) if text
                        ]
                        if rows:
                            return rows
                        await asyncio.sleep(1)
                    return []

                traces = asyncio.run(_run())

            assert traces
            assert any(
                row.get("trace_complete") and row.get("hop_sequence") for row in traces
            )
        finally:
            self._close_session(session_id)
