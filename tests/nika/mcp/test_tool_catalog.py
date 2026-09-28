"""Agent-visible MCP tool catalog after command aliases are consolidated."""

from __future__ import annotations

import pytest

from nika.mcp.k8s.server import mcp as k8s_mcp
from nika.mcp.registry import select_diagnosis_servers
from nika.mcp.servers.common.host_server import mcp as host_mcp
from nika.mcp.servers.containerlab.srl_server import mcp as srl_mcp
from nika.mcp.servers.kathara.frr_server import mcp as frr_mcp
from nika.mcp.servers.kathara.iosxr_server import mcp as iosxr_mcp
from nika.mcp.servers.kathara.routeros_server import mcp as routeros_mcp
from nika.mcp.servers.kathara.sdn_server import mcp as sdn_mcp


@pytest.mark.asyncio
async def test_generic_and_specialized_tool_catalog() -> None:
    expected = (
        (host_mcp, {"exec_shell", "curl_web_test", "iperf_test", "active_tcp_probe"}),
        (k8s_mcp, {"k8s_list_events"}),
        (frr_mcp, {"frr_get_rpki_status"}),
        (iosxr_mcp, {"iosxr_exec"}),
        (routeros_mcp, {"routeros_exec"}),
        (srl_mcp, {"srl_exec_cli"}),
        (sdn_mcp, {"sdn_onos_rest"}),
    )
    for server, names in expected:
        assert {tool.name for tool in await server.list_tools()} == names


def test_frr_server_only_selected_for_rpki() -> None:
    assert "kathara_frr_mcp_server" not in select_diagnosis_servers(
        "simple_bgp", backend="kathara"
    )
    assert "kathara_frr_mcp_server" in select_diagnosis_servers(
        "isp_abilene_ebgp_rpki", backend="kathara"
    )
