"""Agent-visible MCP tool catalog after command aliases are consolidated."""

from __future__ import annotations

import pytest

from nika.mcp.k8s.server import mcp as k8s_mcp
from nika.mcp.registry import select_diagnosis_servers
from nika.mcp.servers.common.host_server import mcp as host_mcp
from nika.mcp.gateway.access import TOOL_NODE_ARGUMENTS
from nika.mcp.servers.containerlab.sonic_server import mcp as sonic_mcp
from nika.mcp.servers.containerlab.srl_server import mcp as srl_mcp
from nika.mcp.servers.kathara.frr_server import mcp as frr_mcp
from nika.mcp.servers.kathara.iosxr_server import mcp as iosxr_mcp
from nika.mcp.servers.kathara.netflow_server import mcp as netflow_mcp
from nika.mcp.servers.kathara.prometheus_server import mcp as prometheus_mcp
from nika.mcp.servers.kathara.syslog_server import mcp as syslog_mcp
from nika.mcp.servers.kathara.routeros_server import mcp as routeros_mcp
from nika.mcp.servers.kathara.sdn_server import mcp as sdn_mcp

pytestmark = pytest.mark.contract


@pytest.mark.asyncio
async def test_generic_and_specialized_tool_catalog() -> None:
    expected = (
        (
            host_mcp,
            {
                "exec_shell",
                "device_read",
                "device_interface_state",
                "device_protocol_counters",
                "curl_web_test",
                "iperf_test",
                "active_tcp_probe",
            },
        ),
        (k8s_mcp, {"k8s_list_events"}),
        (frr_mcp, {"frr_get_rpki_status"}),
        (iosxr_mcp, {"iosxr_exec"}),
        (routeros_mcp, {"routeros_exec"}),
        (srl_mcp, {"srl_exec_cli"}),
        (sonic_mcp, {"sonic_exec_cli"}),
        (sdn_mcp, {"sdn_onos_rest"}),
        (
            netflow_mcp,
            {
                "flow_coverage",
                "flow_timeline",
                "flow_current_load",
                "flow_top_talkers",
                "flow_conversations",
                "flow_records",
                "nfdump",
            },
        ),
        (prometheus_mcp, {"prometheus_query"}),
        (syslog_mcp, {"syslog_query"}),
    )
    for server, names in expected:
        assert {tool.name for tool in await server.list_tools()} == names


def test_containerlab_routers_get_their_platform_cli_server() -> None:
    def vendor(scenario: str, device_profile: str | None) -> set[str]:
        servers = select_diagnosis_servers(
            scenario, backend="containerlab", device_profile=device_profile
        )
        return {s for s in servers if s.startswith("containerlab_")}

    for scenario in ("dc_clos", "isp_dfn-bwin"):
        assert vendor(scenario, None) == {"containerlab_srl_mcp_server"}
        assert vendor(scenario, "nokia_srlinux") == {"containerlab_srl_mcp_server"}
        assert vendor(scenario, "sonic") == {"containerlab_sonic_mcp_server"}
    assert TOOL_NODE_ARGUMENTS["sonic_exec_cli"] == ("device_name",)


def test_frr_server_only_selected_for_rpki() -> None:
    assert "kathara_frr_mcp_server" not in select_diagnosis_servers(
        "simple_bgp", backend="kathara"
    )
    assert "kathara_frr_mcp_server" in select_diagnosis_servers(
        "isp_abilene", backend="kathara", deployment={"bgp_mode": "ebgp"}
    )


@pytest.mark.parametrize(
    ("deployment", "device_profile", "expected"),
    [
        # Current FRR eBGP deployments carry RPKI/ROV.
        ({"scenario_params": {"bgp_mode": "ebgp"}}, None, True),
        ({"scenario_params": {"bgp_mode": "ibgp_rr"}}, None, False),
        ({"scenario_params": {"bgp_mode": "ebgp"}}, "iosxr", False),
        # Release metadata does not change deployment-based tool selection.
        (
            {"scenario_params": {"bgp_mode": "ebgp"}},
            None,
            True,
        ),
    ],
)
def test_frr_server_follows_the_deployed_rpki_profile(
    deployment, device_profile, expected
) -> None:
    servers = select_diagnosis_servers(
        "isp_polska",
        backend="kathara",
        device_profile=device_profile,
        deployment=deployment,
    )
    assert ("kathara_frr_mcp_server" in servers) is expected


def test_telemetry_servers_only_selected_with_session_telemetry() -> None:
    def telemetry_servers(telemetry: tuple[str, ...]) -> set[str]:
        servers = select_diagnosis_servers(
            "isp_abilene",
            backend="kathara",
            deployment={"bgp_mode": "ebgp"},
            telemetry=telemetry,
        )
        return {
            s
            for s in servers
            if s.endswith(
                ("netflow_mcp_server", "prometheus_mcp_server", "syslog_mcp_server")
            )
        }

    assert telemetry_servers(()) == set()
    assert telemetry_servers(("netflow",)) == {
        "kathara_netflow_mcp_server",
        "kathara_prometheus_mcp_server",
    }
    assert telemetry_servers(("pingmesh",)) == {"kathara_prometheus_mcp_server"}
    assert telemetry_servers(("prometheus",)) == {"kathara_prometheus_mcp_server"}
    assert telemetry_servers(("syslog",)) == {"kathara_syslog_mcp_server"}
    assert "kathara_netflow_mcp_server" in select_diagnosis_servers(
        "sdn_l3_clos", backend="kathara", telemetry=("netflow",)
    )
    assert "kathara_prometheus_mcp_server" in select_diagnosis_servers(
        "p4_dc_fabric", backend="kathara", telemetry=("prometheus",)
    )
    assert "kathara_telemetry_mcp_server" in select_diagnosis_servers(
        "p4_dc_gateway", backend="kathara"
    )
    assert "kathara_netflow_mcp_server" in select_diagnosis_servers(
        "dc_clos", backend="containerlab", telemetry=("netflow",)
    )
    assert "kathara_prometheus_mcp_server" in select_diagnosis_servers(
        "dc_clos", backend="containerlab", telemetry=("prometheus",)
    )
    assert "kathara_netflow_mcp_server" in select_diagnosis_servers(
        "isp_abilene", backend="containerlab", telemetry=("netflow",)
    )
    assert "kathara_prometheus_mcp_server" in select_diagnosis_servers(
        "dc_clos", device_profile="iosxr", backend="kathara", telemetry=("prometheus",)
    )
    assert "kathara_netflow_mcp_server" in select_diagnosis_servers(
        "dc_clos", device_profile="routeros", backend="kathara", telemetry=("netflow",)
    )
