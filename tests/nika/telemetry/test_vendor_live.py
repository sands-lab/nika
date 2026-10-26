"""Live Kathara vendor routers: case YAML drives IOS XR and RouterOS telemetry."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import load_session_mcp_config
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.net_env.utils.iosxr.common import IMAGE as XRD_IMAGE
from nika.net_env.utils.routeros.common import IMAGE as ROS_IMAGE
from nika.service.lab.routeros_api import CLI_COMMAND
from nika.runtime.factory import runtime_for_session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available, docker_image_available

pytestmark = pytest.mark.integration

_PC2 = "200.1.1.2"


def _tool_rows(name: str, raw: object) -> list[dict]:
    rows: list[dict] = []
    for text in tool_text_list(raw):
        assert text, f"{name} returned empty output"
        lowered = text.lower()
        for bad in ("traceback", "tool_execution_error", "nika_session_id is not set"):
            assert bad not in lowered, f"{name} looks like an error:\n{text[:800]}"
        rows.append(json.loads(text))
    return rows


def _poll(deadline: float, interval: float, probe: Callable[[], object]) -> object:
    value = probe()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = probe()
    return value


class _VendorTelemetryLive(IntegrationTestCase):
    scenario: str
    telemetry: tuple[str, ...]
    metric: str
    extra_metrics: tuple[str, ...] = ()
    log_command: str = ""
    version_prefix: str
    routers: tuple[str, ...] = ("router1", "router2")

    def test_yaml_enables_vendor_telemetry(self) -> None:
        session_id = None
        try:
            session_id = start_net_env(
                self.scenario,
                None,
                session_tag=resolve_session_tag(context="test"),
                telemetry=self.telemetry,
            )
            session = self._assert_session_ready(session_id, self.scenario)
            params = session.get("scenario_params") or {}
            assert params.get("telemetry") == list(self.telemetry)
            plan = (session.get("metadata") or {}).get("telemetry") or {}
            runtime = runtime_for_session(session)
            prom_versions = plan["kinds"]["prometheus"]["versions"]
            for router in self.routers:
                assert prom_versions[router].startswith(self.version_prefix)
            if "netflow" in self.telemetry:
                netflow_versions = plan["kinds"]["netflow"]["versions"]
                for router in self.routers:
                    if self.version_prefix == "xr-":
                        assert router not in netflow_versions
                    else:
                        assert netflow_versions[router].startswith(self.version_prefix)

            runtime.exec(
                "pc1",
                "nohup sh -c 'while true; do "
                f"ping -f -c 400 -W 1 {_PC2} || "
                f"ping -q -c 400 -i 0.01 -W 1 {_PC2}; "
                "done' >/dev/null 2>&1 &",
            )

            with mcp_gateway_for_session(session_id, scenario_name=self.scenario):
                config = load_session_mcp_config(session_id, self.scenario)
                assert "kathara_prometheus_mcp_server" in config

                async def _prom(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "prometheus_query",
                        await tools["prometheus_query"].ainvoke(args),
                    )

                counters = asyncio.run(
                    _prom(query=f'{self.metric}{{node="router1"}}', limit=50)
                )
                assert counters
                assert any(item["value"] >= 0 for item in counters)
                assert "kathara_syslog_mcp_server" in config
                if self.log_command:
                    runtime.exec("router1", self.log_command)

                    async def _syslog(**args) -> list[dict]:
                        client = MultiServerMCPClient(connections=config)
                        tools = {t.name: t for t in await client.get_tools()}
                        return _tool_rows(
                            "syslog_query",
                            await tools["syslog_query"].ainvoke(args),
                        )

                    logs = _poll(
                        time.monotonic() + 30,
                        2,
                        lambda: asyncio.run(
                            _syslog(start_time="-180", host="router1", limit=50)
                        ),
                    )
                    assert logs
                for metric in self.extra_metrics:
                    series = asyncio.run(
                        _prom(query=f'{metric}{{node="router1"}}', limit=50)
                    )
                    assert series, metric

                if "netflow" not in self.telemetry:
                    return
                assert "kathara_netflow_mcp_server" in config

                async def _netflow(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "netflow_query", await tools["netflow_query"].ainvoke(args)
                    )

                def _exporters() -> set[str]:
                    rows = asyncio.run(
                        _netflow(start_time="-180", aggregate_by=["router"])
                    )
                    return {
                        item.get("exporter") for item in rows if item.get("exporter")
                    }

                exporters = _poll(time.monotonic() + 60, 10, _exporters)
            assert exporters is not None
            if exporters:
                assert exporters & set(self.routers)
        finally:
            if session_id:
                self._close_session(session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker is not available")
@pytest.mark.skipif(
    not docker_image_available(ROS_IMAGE),
    reason="RouterOS vrnetlab image not available",
)
class TestRouterOsTelemetryYamlLive(_VendorTelemetryLive):
    """routeros_simple_bgp YAML: traffic-flow and SSH interface counters."""

    scenario = "routeros_simple_bgp"
    telemetry = ("netflow", "prometheus", "syslog")
    log_command = CLI_COMMAND.format(
        command="/log info message=neighbor-193.10.11.2-Up"
    )
    metric = "routeros_interface_rx_packets"
    extra_metrics = (
        "routeros_interface_up",
        "routeros_interface_rx_errors",
        "routeros_bgp_peer_up",
    )
    version_prefix = "ros-"


@pytest.mark.skipif(not docker_available(), reason="Docker is not available")
@pytest.mark.skipif(
    not docker_image_available(XRD_IMAGE),
    reason="XRd Control Plane image not available",
)
class TestIosXrTelemetryYamlLive(_VendorTelemetryLive):
    """iosxr_simple_bgp YAML: xr_cli interface counters. No IPFIX on XRd CP."""

    scenario = "iosxr_simple_bgp"
    telemetry = ("prometheus", "syslog")
    log_command = "logger -p local7.info -t bgpd neighbor-193.10.11.2-Up || true"
    metric = "xr_interface_in_packets"
    extra_metrics = (
        "xr_interface_up",
        "xr_interface_in_errors",
        "xr_bgp_peer_up",
    )
    version_prefix = "xr-"
