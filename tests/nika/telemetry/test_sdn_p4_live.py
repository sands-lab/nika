"""Live SDN/P4 labs: case YAML and nika.telemetry.subnet drive collection."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import load_session_mcp_config
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from nika.net_env.sdn_l3_clos.topology_model import (
    build_clos_fabric_model as build_sdn_model,
)
from nika.net_env.utils.kathara.docker_files.docker_images import KATHARA_P4_IMAGE
from nika.run_config.loader import load_run_config, reset_run_config, set_run_config
from nika.runtime.factory import runtime_for_session
from nika.telemetry.management import TELEMETRY_COLLECTOR
from nika.utils.session_id import resolve_session_tag
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.env.start import start_net_env
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available, docker_image_available

pytestmark = pytest.mark.integration

SUBNET = "172.26.0.0/16"


def _tool_rows(name: str, raw: object) -> list[dict]:
    rows: list[dict] = []
    for text in tool_text_list(raw):
        assert text, f"{name} returned empty output"
        lowered = text.lower()
        for bad in ("traceback", "tool_execution_error", "nika_session_id is not set"):
            assert bad not in lowered, f"{name} looks like an error:\n{text[:800]}"
        rows.append(json.loads(text))
    return rows


def _int_rows(raw: object) -> list[dict]:
    if isinstance(raw, str):
        if not raw.strip():
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]
    if (
        isinstance(raw, list)
        and raw
        and isinstance(raw[0], dict)
        and (
            "packet_timestamp" in raw[0]
            or "trace_complete" in raw[0]
            or "hop_sequence" in raw[0]
        )
    ):
        return raw
    rows: list[dict] = []
    for text in tool_text_list(raw):
        if not str(text).strip():
            continue
        item = json.loads(text)
        rows.extend(item if isinstance(item, list) else [item])
    return rows


def _write_case_yaml(path: Path, scenario: str, telemetry: list[str]) -> Path:
    kinds = ", ".join(telemetry)
    path.write_text(
        "cases:\n"
        f"  - scenario: {scenario}\n"
        "    problem: healthy\n"
        "    topo_size: s\n"
        f"    telemetry: [{kinds}]\n"
        "    inject: {}\n"
    )
    return path


def _apply_subnet_yaml(path: Path) -> None:
    path.write_text("nika:\n  telemetry:\n    subnet: 172.26.0.0/16\n")
    set_run_config(load_run_config(path))


def _poll(deadline: float, interval: float, probe: Callable[[], object]) -> object:
    value = probe()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = probe()
    return value


class _YamlTelemetryLive(IntegrationTestCase):
    """Start a lab from case YAML plus nika.telemetry.subnet."""

    scenario: str
    telemetry: tuple[str, ...]

    def _start_from_yaml(self, tmp_path: Path) -> tuple[str, dict]:
        case_path = _write_case_yaml(
            tmp_path / "cases.yaml", self.scenario, list(self.telemetry)
        )
        row = load_benchmark_yaml(case_path)[0]
        assert row["telemetry"] == list(self.telemetry)
        _apply_subnet_yaml(tmp_path / "nika.yaml")
        session_id = start_net_env(
            row["scenario"],
            row["topo_size"],
            session_tag=resolve_session_tag(context="test"),
            telemetry=row["telemetry"],
        )
        return session_id, self._assert_session_ready(session_id, self.scenario)

    def _assert_yaml_took_effect(self, session_row: dict) -> dict:
        params = session_row.get("scenario_params") or {}
        assert params.get("telemetry") == list(self.telemetry)
        plan = (session_row.get("metadata") or {}).get("telemetry") or {}
        assert plan.get("subnet") == SUBNET
        if "syslog" in self.telemetry:
            assert "syslog" in (plan.get("kinds") or {})
        runtime = runtime_for_session(session_row)
        addr = runtime.exec(TELEMETRY_COLLECTOR, "ip -4 -o addr show")
        assert "172.26." in addr
        return plan

    def _assert_syslog(self, runtime, config: dict, node: str) -> None:
        runtime.exec(
            node, "logger -p local7.info -t bgpd 'neighbor 10.0.0.2 Up' || true"
        )

        async def _syslog(**args) -> list[dict]:
            client = MultiServerMCPClient(connections=config)
            tools = {t.name: t for t in await client.get_tools()}
            return _tool_rows("syslog_query", await tools["syslog_query"].ainvoke(args))

        logs = _poll(
            time.monotonic() + 30,
            2,
            lambda: asyncio.run(_syslog(start_time="-180", host=node, limit=50)),
        )
        assert logs


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestSdnTelemetryYamlLive(_YamlTelemetryLive):
    """sdn_l3_clos YAML: OVS NetFlow and OpenFlow port metrics."""

    scenario = "sdn_l3_clos"
    telemetry = ("netflow", "prometheus", "syslog")

    def test_yaml_enables_ovs_netflow_and_openflow_metrics(
        self, tmp_path: Path
    ) -> None:
        session_id = None
        try:
            session_id, row = self._start_from_yaml(tmp_path)
            plan = self._assert_yaml_took_effect(row)
            netflow_versions = plan["kinds"]["netflow"]["versions"]
            prom_versions = plan["kinds"]["prometheus"]["versions"]
            assert netflow_versions["leaf_1"].startswith("ovs-")
            assert prom_versions["leaf_1"].startswith("ovs-")
            assert any(
                not version.startswith("ovs-") for version in prom_versions.values()
            )

            model = build_sdn_model("s")
            src = model.client_endpoints()[0]
            dst = next(
                web for web in model.web_endpoints() if web.leaf_id != src.leaf_id
            )
            runtime = runtime_for_session(row)
            runtime.exec(
                src.name,
                "nohup sh -c 'while true; do ping -q -c 5 -i 0.2 -W 1 "
                f"{dst.ip}; done' >/dev/null 2>&1 &",
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

                ports = asyncio.run(
                    _prom(query='openflow_port_rx_packets{node="leaf_1"}', limit=50)
                )
                assert ports
                assert any(row["value"] >= 0 for row in ports)
                self._assert_syslog(runtime, config, "leaf_1")

                def _exporters() -> set[str]:
                    rows = asyncio.run(
                        _netflow(
                            start_time="-120",
                            filter=f"src ip {src.ip} and dst ip {dst.ip}",
                            aggregate_by=["router"],
                        )
                    )
                    return {item["exporter"] for item in rows}

                exporters = _poll(time.monotonic() + 180, 10, _exporters)
            assert "leaf_1" in exporters
            assert "leaf_" in "".join(exporters)
        finally:
            reset_run_config()
            if session_id:
                self._close_session(session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
@pytest.mark.skipif(
    not docker_image_available(KATHARA_P4_IMAGE),
    reason="kathara/p4 image not available",
)
class TestP4FabricTelemetryYamlLive(_YamlTelemetryLive):
    """p4_dc_fabric YAML: P4Runtime counters in Prometheus."""

    scenario = "p4_dc_fabric"
    telemetry = ("prometheus", "syslog")

    def test_yaml_enables_p4_counter_metrics(self, tmp_path: Path) -> None:
        session_id = None
        try:
            session_id, row = self._start_from_yaml(tmp_path)
            plan = self._assert_yaml_took_effect(row)
            versions = plan["kinds"]["prometheus"]["versions"]
            assert "p4" in versions.get("fabric_mgr", "").split("+")

            model = build_clos_fabric_model("s")
            src = model.client_endpoints()[0]
            dst = next(
                web for web in model.web_endpoints() if web.leaf_id != src.leaf_id
            )
            runtime = runtime_for_session(row)
            runtime.exec(src.name, f"ping -c 3 -W 1 {dst.ip} || true")

            with mcp_gateway_for_session(session_id, scenario_name=self.scenario):
                config = load_session_mcp_config(session_id, self.scenario)
                assert "kathara_prometheus_mcp_server" in config
                assert "kathara_syslog_mcp_server" in config
                assert "kathara_netflow_mcp_server" not in config

                async def _prom(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    return _tool_rows(
                        "prometheus_query",
                        await tools["prometheus_query"].ainvoke(args),
                    )

                def _counters() -> list[dict]:
                    return asyncio.run(_prom(query="p4_ingress_packets", limit=100))

                counters = _poll(time.monotonic() + 90, 5, _counters)
                self._assert_syslog(runtime, config, "fabric_mgr")
            assert counters
            assert {row["node"] for row in counters} & set(model.leaves + model.spines)
        finally:
            reset_run_config()
            if session_id:
                self._close_session(session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
@pytest.mark.skipif(
    not docker_image_available(KATHARA_P4_IMAGE),
    reason="kathara/p4 image not available",
)
class TestP4GatewayTelemetryYamlLive(_YamlTelemetryLive):
    """p4_dc_gateway YAML: Prometheus counters plus always-on INT-MX."""

    scenario = "p4_dc_gateway"
    telemetry = ("prometheus", "syslog")

    def test_yaml_prometheus_and_int_traces(self, tmp_path: Path) -> None:
        session_id = None
        try:
            session_id, row = self._start_from_yaml(tmp_path)
            plan = self._assert_yaml_took_effect(row)
            versions = plan["kinds"]["prometheus"]["versions"]
            assert "p4" in versions.get("fabric_mgr", "").split("+")

            model = build_gateway_fabric_model("s")
            runtime = runtime_for_session(row)
            runtime.exec(
                model.clients[0].name,
                "nohup sh -c 'while true; do "
                f"nc -z -w 1 {model.services[0].ip} 80; sleep 1; done' "
                ">/dev/null 2>&1 &",
            )

            with mcp_gateway_for_session(session_id, scenario_name=self.scenario):
                config = load_session_mcp_config(session_id, self.scenario)
                assert "kathara_prometheus_mcp_server" in config
                assert "kathara_syslog_mcp_server" in config
                assert "kathara_telemetry_mcp_server" in config

                async def _prom(**args) -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    assert "prometheus_query" in tools
                    return _tool_rows(
                        "prometheus_query",
                        await tools["prometheus_query"].ainvoke(args),
                    )

                async def _wait_traces() -> list[dict]:
                    client = MultiServerMCPClient(connections=config)
                    tools = {t.name: t for t in await client.get_tools()}
                    assert "int_query_telemetry" in tools
                    deadline = time.monotonic() + 90
                    while True:
                        raw = await tools["int_query_telemetry"].ainvoke(
                            {"start_time": "0", "limit": 20}
                        )
                        traces = _int_rows(raw)
                        if traces or time.monotonic() >= deadline:
                            return traces
                        await asyncio.sleep(1)

                counters = _poll(
                    time.monotonic() + 90,
                    5,
                    lambda: asyncio.run(_prom(query="p4_ingress_packets", limit=100)),
                )
                traces = asyncio.run(_wait_traces())
                self._assert_syslog(runtime, config, "fabric_mgr")
            assert counters
            assert traces
            assert any(
                row.get("trace_complete") and row.get("hop_sequence") for row in traces
            )
        finally:
            reset_run_config()
            if session_id:
                self._close_session(session_id)
