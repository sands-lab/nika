"""Live MCP gateway invocation for SDN Clos tools on sdn_l3_clos."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from uuid import uuid4

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import ToolException

from nika.agents.mcp_config import MCPServerConfig
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.net_env.sdn_l3_clos.topology_model import ONOS_OOB_IP, ONOS_REST_PORT
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.workflows.failure.inject import prepare_failure
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration


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

    def test_mcp_sdn_tools_return_usable_output(self, tmp_path) -> None:
        session_id = self._start_env("sdn_l3_clos", ["-s", "s"])
        failure = None
        runtime = None
        try:
            self._assert_session_ready(session_id, "sdn_l3_clos")
            session = Session().load_running_session(session_id=session_id)
            runtime = runtime_for_session(session)
            peer_ip = runtime.get_host_ip("client_2_1", "eth0")
            probe = f"ping -n -c 12 -i 0.2 -W 1 {peer_ip}"
            assert "0% packet loss" in runtime.exec("client_1_1", probe)

            with mcp_gateway_for_session(session_id, scenario_name="sdn_l3_clos"):
                config = MCPServerConfig(session_id=session_id).load_http_config(
                    ["kathara_sdn_mcp_server", "kathara_base_mcp_server"]
                )

                async def _run() -> dict[str, str]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {tool.name: tool for tool in await client.get_tools()}
                    assert "sdn_onos_rest" in tools and "exec_shell" in tools
                    assert "sdn_ovs_exec" not in tools
                    assert "sdn_controller_logs" not in tools
                    with pytest.raises(ToolException, match=r"HTTP 404") as invalid:
                        await tools["sdn_onos_rest"].ainvoke(
                            {"path": "/onos/v1/nonexistent-nika-test-endpoint"}
                        )
                    (tmp_path / "http-404.txt").write_text(str(invalid.value))

                    return {
                        "devices": _text(
                            await tools["sdn_onos_rest"].ainvoke(
                                {"path": "/onos/v1/devices"}
                            )
                        ),
                        "flows": _text(
                            await tools["exec_shell"].ainvoke(
                                {
                                    "host_name": "leaf_1",
                                    "command": (
                                        "ovs-ofctl -O OpenFlow13 dump-flows leaf_1"
                                    ),
                                }
                            )
                        ),
                        "logs": _text(
                            await tools["exec_shell"].ainvoke(
                                {
                                    "host_name": "onos",
                                    "command": "tail -n 20 /root/onos/apache-karaf-*/data/log/karaf.log",
                                }
                            )
                        ),
                    }

                results = asyncio.run(_run())
                stages = {"healthy": results}
                failure = prepare_failure(
                    session,
                    ["link_flap"],
                    {
                        "host_name": "client_1_1",
                        "intf_name": "eth0",
                        "down_time": 1,
                        "up_time": 1,
                    },
                )
                failure.inject()
                assert failure.verify()["verified"]
                stages["fault"] = asyncio.run(_run())
                inspection = subprocess.run(
                    [
                        "uv",
                        "run",
                        "nika",
                        "exec",
                        "--session_id",
                        session_id,
                        "client_1_1",
                        f"ip -br link show eth0; {probe}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                ).stdout
                (tmp_path / "link-flap-inspection.txt").write_text(inspection)
                loss = re.search(r"([\d.]+)% packet loss", inspection)
                assert loss and 0 < float(loss[1]) < 100, inspection
                assert failure.recover()["verified"]
                failure = None
                stages["recovered"] = asyncio.run(_run())
                assert "0% packet loss" in runtime.exec("client_1_1", probe)
                (tmp_path / "mcp-stages.json").write_text(json.dumps(stages, indent=2))
                for stage, outputs in stages.items():
                    devices = json.loads(outputs["devices"])
                    assert devices["body"]["devices"], (stage, devices)
                    for tool, output in outputs.items():
                        _assert_tool_ok(f"{stage} {tool}", output)

                table = f"probe_{uuid4().hex[:8]}"
                try:
                    runtime.exec(
                        "fabric_mgr",
                        f"nft add table inet {table}; "
                        f"nft 'add chain inet {table} output {{ type filter hook output priority 0; policy accept; }}'; "
                        f"nft add rule inet {table} output ip daddr {ONOS_OOB_IP} "
                        f"tcp dport {ONOS_REST_PORT} reject",
                    )
                    legacy = runtime.exec(
                        "fabric_mgr",
                        f"curl -s -u onos:rocks --connect-timeout 3 "
                        f"http://{ONOS_OOB_IP}:{ONOS_REST_PORT}/onos/v1/devices || echo '{{}}'",
                    )
                    assert json.loads(legacy) == {}

                    async def unavailable():
                        client = MultiServerMCPClient(connections=config)
                        tools = {tool.name: tool for tool in await client.get_tools()}
                        with pytest.raises(ToolException, match=r"HTTP 000") as error:
                            await tools["sdn_onos_rest"].ainvoke(
                                {"path": "/onos/v1/devices"}
                            )
                        return str(error.value)

                    error = asyncio.run(unavailable())
                    (tmp_path / "connection-error.json").write_text(
                        json.dumps(
                            {"legacy_output": legacy, "tool_error": error}, indent=2
                        )
                    )
                finally:
                    runtime.exec("fabric_mgr", f"nft delete table inet {table}")
                restored = asyncio.run(_run())
                assert json.loads(restored["devices"])["body"]["devices"]

            devices = json.loads(results["devices"])
            assert devices["path"] == "/onos/v1/devices"
            assert "body" in devices
            assert "OFPST_FLOW" in results["flows"] or "cookie=" in results["flows"]
            _assert_tool_ok(
                "sdn_onos_rest",
                results["devices"],
                must_contain=("onos_oob", "body"),
            )
            _assert_tool_ok("exec_shell flows", results["flows"])
            _assert_tool_ok("exec_shell logs", results["logs"])
        finally:
            try:
                if failure is not None:
                    failure.recover()
            finally:
                self._close_session(session_id)
                if runtime is not None:
                    assert not runtime.exists()
                    assert not runtime.has_leftover_resources()
