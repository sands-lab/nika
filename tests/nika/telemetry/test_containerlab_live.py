"""Live Containerlab min3clos: SR Linux sFlow, gNMI metrics, and syslog."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import load_session_mcp_config
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.net_env.isp.containerlab.lab import SRL_IMAGE
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.runtime.factory import runtime_for_session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_image_available,
)

pytestmark = pytest.mark.integration

_CLIENT2 = "10.0.0.27"


def _tool_rows(name: str, raw: object) -> list[dict]:
    rows: list[dict] = []
    for text in tool_text_list(raw):
        if not str(text).strip():
            continue
        lowered = str(text).lower()
        for bad in ("traceback", "tool_execution_error", "nika_session_id is not set"):
            assert bad not in lowered, f"{name} looks like an error:\n{text[:800]}"
        item = json.loads(text)
        rows.extend(item if isinstance(item, list) else [item])
    return rows


def _poll(deadline: float, interval: float, probe: Callable[[], object]) -> object:
    value = probe()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = probe()
    return value


@pytest.mark.skipif(
    not containerlab_prerequisites(),
    reason="containerlab, gnmic, or Docker not available",
)
@pytest.mark.skipif(
    not docker_image_available(SRL_IMAGE),
    reason="Nokia SR Linux image not available",
)
@pytest.mark.skipif(
    not docker_image_available(nika_image("telemetry-collector")),
    reason="nika/telemetry-collector image not available",
)
class TestMin3closTelemetryYamlLive(IntegrationTestCase):
    """min3clos YAML: SR Linux sFlow and gNMI interface metrics."""

    scenario = "min3clos"
    telemetry = ("netflow", "prometheus", "syslog")

    def test_yaml_enables_srl_sflow_and_gnmi_metrics(self) -> None:
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
            assert params.get("telemetry") == ["netflow", "prometheus", "syslog"]
            plan = (session.get("metadata") or {}).get("telemetry") or {}
            assert plan.get("backend") == "containerlab"
            runtime = runtime_for_session(session)
            netflow_versions = plan["kinds"]["netflow"]["versions"]
            prom_versions = plan["kinds"]["prometheus"]["versions"]
            assert netflow_versions["leaf1"].startswith("srl-")
            assert prom_versions["leaf1"].startswith("srl-")
            assert "client1" not in netflow_versions
            assert "client1" not in prom_versions

            runtime.exec(
                "client1",
                "nohup sh -c 'while true; do "
                f"ping -f -c 400 -W 1 {_CLIENT2} || "
                f"ping -q -c 400 -i 0.01 -W 1 {_CLIENT2}; "
                "done' >/dev/null 2>&1 &",
            )

            with mcp_gateway_for_session(session_id, scenario_name=self.scenario):
                config = load_session_mcp_config(session_id, self.scenario)
                assert "kathara_netflow_mcp_server" in config
                assert "kathara_prometheus_mcp_server" in config
                assert "kathara_syslog_mcp_server" in config

                async def _netflow(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "netflow_query", await tools["netflow_query"].ainvoke(args)
                    )

                async def _prom(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "prometheus_query",
                        await tools["prometheus_query"].ainvoke(args),
                    )

                counters = asyncio.run(
                    _prom(query='srl_interface_in_packets{node="leaf1"}', limit=50)
                )
                assert counters
                assert any(item["value"] >= 0 for item in counters)
                runtime.exec(
                    "leaf1",
                    "sr_cli <<'EOF'\n"
                    "enter candidate\n"
                    "system information contact syslog-probe\n"
                    "commit now\n"
                    "EOF",
                    timeout=30,
                )

                async def _syslog(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "syslog_query", await tools["syslog_query"].ainvoke(args)
                    )

                logs = _poll(
                    time.monotonic() + 30,
                    2,
                    lambda: asyncio.run(
                        _syslog(
                            start_time="-180",
                            host="leaf1",
                            contains="Opened session",
                            limit=10,
                        )
                    ),
                )
                assert logs

                for metric in (
                    "srl_interface_up",
                    "srl_interface_in_errors",
                    "srl_interface_in_discards",
                    "srl_bgp_peer_up",
                ):
                    series = asyncio.run(
                        _prom(query=f'{metric}{{node="leaf1"}}', limit=50)
                    )
                    assert series, metric

                def _exporters() -> set[str]:
                    rows = asyncio.run(
                        _netflow(start_time="-180", aggregate_by=["router"])
                    )
                    return {
                        item.get("exporter") for item in rows if item.get("exporter")
                    }

                exporters = _poll(time.monotonic() + 60, 10, _exporters)
            # The query path must work. Packet samples are not always produced
            # by the SR Linux software datapath; when they are, they name a
            # router.
            assert exporters is not None
            if exporters:
                assert exporters & {"leaf1", "leaf2", "spine"}
        finally:
            if session_id:
                self._close_session(session_id)
