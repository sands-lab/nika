"""Live Kathara FRR dc_clos: node_exporter plus BGP summary in Prometheus."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import load_session_mcp_config
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.run_config.loader import load_run_config, reset_run_config, set_run_config
from nika.runtime.factory import runtime_for_session
from nika.telemetry.management import TELEMETRY_COLLECTOR
from nika.utils.session_id import resolve_session_tag
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.env.start import start_net_env
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration

SUBNET = "172.26.0.0/16"
_LEAF = "leaf_router_0_0"


def _tool_rows(name: str, raw: object) -> list[dict]:
    rows: list[dict] = []
    for text in tool_text_list(raw):
        assert text, f"{name} returned empty output"
        lowered = text.lower()
        for bad in ("traceback", "tool_execution_error", "nika_session_id is not set"):
            assert bad not in lowered, f"{name} looks like an error:\n{text[:800]}"
        rows.append(json.loads(text))
    return rows


@pytest.mark.skipif(not docker_available(), reason="Docker is not available")
class TestDcClosFrrTelemetryYamlLive(IntegrationTestCase):
    """dc_clos s YAML: kernel interface series and FRR BGP peer gauges."""

    scenario = "dc_clos"
    telemetry = ("netflow", "prometheus", "syslog")

    def test_yaml_enables_frr_interface_and_bgp_metrics(self, tmp_path: Path) -> None:
        session_id = None
        try:
            case_path = tmp_path / "cases.yaml"
            case_path.write_text(
                "cases:\n"
                "  - scenario: dc_clos\n"
                "    problem: healthy\n"
                "    topo_size: s\n"
                "    telemetry: [netflow, prometheus, syslog]\n"
                "    inject: {}\n"
            )
            row = load_benchmark_yaml(case_path)[0]
            (tmp_path / "nika.yaml").write_text(
                "nika:\n  telemetry:\n    subnet: 172.26.0.0/16\n"
            )
            set_run_config(load_run_config(tmp_path / "nika.yaml"))
            session_id = start_net_env(
                row["scenario"],
                row["topo_size"],
                session_tag=resolve_session_tag(context="test"),
                telemetry=row["telemetry"],
            )
            session = self._assert_session_ready(session_id, self.scenario)
            plan = (session.get("metadata") or {}).get("telemetry") or {}
            assert plan.get("subnet") == SUBNET
            runtime = runtime_for_session(session)
            addr = runtime.exec(TELEMETRY_COLLECTOR, "ip -4 -o addr show")
            assert "172.26." in addr
            assert _LEAF in plan["kinds"]["prometheus"]["versions"]
            assert _LEAF in plan["kinds"]["netflow"]["versions"]

            with mcp_gateway_for_session(session_id, scenario_name=self.scenario):
                config = load_session_mcp_config(session_id, self.scenario)
                assert "kathara_prometheus_mcp_server" in config
                assert "kathara_netflow_mcp_server" in config
                assert "kathara_syslog_mcp_server" in config

                async def _prom(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "prometheus_query",
                        await tools["prometheus_query"].ainvoke(args),
                    )

                ifaces = asyncio.run(
                    _prom(
                        query=f'node_network_receive_packets_total{{node="{_LEAF}"}}',
                        limit=50,
                    )
                )
                assert ifaces
                bgp = asyncio.run(
                    _prom(query=f'frr_bgp_peer_up{{node="{_LEAF}"}}', limit=50)
                )
                assert bgp
                assert any(item["value"] in {0, 1} for item in bgp)

                runtime.exec(
                    _LEAF,
                    "logger -p local7.info -t bgpd 'neighbor 10.0.0.2 Up'",
                )

                async def _syslog(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "syslog_query", await tools["syslog_query"].ainvoke(args)
                    )

                deadline = time.monotonic() + 30
                logs: list[dict] = []
                while time.monotonic() < deadline:
                    logs = asyncio.run(
                        _syslog(
                            start_time="-180",
                            host=_LEAF,
                            contains="neighbor",
                            limit=50,
                        )
                    )
                    if logs:
                        break
                    time.sleep(2)
                assert logs
        finally:
            reset_run_config()
            if session_id:
                self._close_session(session_id)
