"""Device CLI writes, selected INT flows, and native trial isolation."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient
from typer.testing import CliRunner

from nika.agents.mcp_config import load_session_mcp_config
from nika.cli.main import app
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from tests.support.integration_base import IntegrationTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.e2e


def _exec(session_id, node, command, timeout=30):
    args = [
        "exec",
        "--session_id",
        session_id,
        "--timeout",
        str(timeout),
        node,
        command,
    ]
    result = CliRunner().invoke(app, args)
    print("nika " + " ".join(args), "\n" + result.output, flush=True)
    assert result.exit_code == 0, result.output
    return result.output


def _value(raw):
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    if isinstance(raw, dict):
        return raw
    if (
        isinstance(raw, list)
        and raw
        and isinstance(raw[0], dict)
        and "text" not in raw[0]
    ):
        return raw
    values = [_value(text) for text in tool_text_list(raw)]
    return values[0] if len(values) == 1 else values


async def _tools(session_id, scenario):
    client = MultiServerMCPClient(
        connections=load_session_mcp_config(session_id, scenario)
    )
    return {tool.name: tool for tool in await client.get_tools()}


async def _call(tools, name, **args):
    result = _value(await tools[name].ainvoke(args))
    if name == "prometheus_query" and isinstance(result, dict) and "metric" in result:
        result = [result]
    print(json.dumps({"tool": name, "arguments": args, "result": result}), flush=True)
    assert not isinstance(result, dict) or not result.get("error"), result
    return result


@pytest.mark.skipif(not docker_available(), reason="Requires Docker/Kathara")
class CliWritesE2ETest(IntegrationTestCase):
    def test_selected_reverse_int_cross_leaf_isolation_and_reset(self):
        model = build_gateway_fabric_model("s")
        session_id = self._start_env(
            "p4_dc_gateway", ["-s", "s", "--telemetry", "prometheus"]
        )
        try:
            row = self._assert_session_ready(session_id, "p4_dc_gateway")
            with mcp_gateway_for_session(
                session_id,
                scenario_name="p4_dc_gateway",
                tool_call_limits={"diagnosis": 200},
            ):

                async def exercise():
                    tools = await _tools(session_id, "p4_dc_gateway")
                    source = model.services[0]
                    target = model.clients[0]
                    sport, dport = 42101, 42102
                    key = (
                        f"--switch {source.attached_switch} --table int_watchlist "
                        f"--match srcAddr={source.ip}/32 --match dstAddr={target.ip}/32 "
                        f"--match ipProtocol=6 --match srcPort={sport} --match dstPort={dport} --priority 20"
                    )
                    start = str(time.time())
                    assert (
                        await _call(
                            tools,
                            "p4rt_exec",
                            args=f"insert {key} --action watch_int",
                        )
                    )["ok"]
                    probe_args = dict(
                        source=source.name,
                        destination=target.name,
                        source_port=sport,
                        destination_port=dport,
                        payload_seed=123,
                        packets=4,
                    )
                    probe = await _call(tools, "active_tcp_probe", **probe_args)
                    assert probe["status"] == "ok", probe
                    traces = []
                    for _ in range(15):
                        traces = (
                            await _call(
                                tools,
                                "int_query_telemetry",
                                start_time=start,
                                src=source.ip,
                                dst=target.ip,
                                src_port=sport,
                                dst_port=dport,
                            )
                        )["records"]
                        if any(
                            r["trace_complete"] and len(r["hop_sequence"]) >= 3
                            for r in traces
                        ):
                            break
                        await asyncio.sleep(1)
                    complete = [r for r in traces if r["trace_complete"]]
                    assert complete, traces
                    assert (
                        complete[0]["hop_sequence"][0]["switch"]
                        == source.attached_switch
                    )
                    assert (
                        complete[0]["hop_sequence"][-1]["switch"]
                        == target.attached_switch
                    )
                    source_egress = complete[0]["hop_sequence"][0]["egress_port"]
                    for _ in range(15):
                        metrics = await _call(
                            tools,
                            "prometheus_query",
                            query=f'p4_int_last_report_timestamp_seconds{{switch="{source.attached_switch}",egress_port="{source_egress}"}}',
                        )
                        if isinstance(metrics, list) and any(
                            float(m["value"]) >= float(start) for m in metrics
                        ):
                            break
                        await asyncio.sleep(1)
                    assert any(float(m["value"]) >= float(start) for m in metrics), (
                        metrics
                    )
                    expected_hops = {
                        (h["switch"], str(h["egress_port"]))
                        for h in complete[0]["hop_sequence"]
                    }
                    for metric in (
                        "p4_int_hop_latency_seconds",
                        "p4_int_queue_depth_packets",
                    ):
                        measurements = await _call(
                            tools, "prometheus_query", query=metric
                        )
                        assert expected_hops <= {
                            (r["metric"]["switch"], r["metric"]["egress_port"])
                            for r in measurements
                        }
                    assert (await _call(tools, "p4rt_exec", args=f"delete {key}"))["ok"]
                    await asyncio.sleep(2)
                    after = str(time.time())
                    assert (await _call(tools, "active_tcp_probe", **probe_args))[
                        "status"
                    ] == "ok"
                    await asyncio.sleep(2)
                    assert not (
                        await _call(
                            tools,
                            "int_query_telemetry",
                            start_time=after,
                            src=source.ip,
                            dst=target.ip,
                            src_port=sport,
                            dst_port=dport,
                        )
                    )["records"]
                    # A second table is writable through the same generic command.
                    assert (
                        await _call(
                            tools,
                            "p4rt_exec",
                            args="modify --switch gateway_1 --table ecn_config --match egress_port=1 --action set_ecn_threshold --param threshold=73",
                        )
                    )["ok"]
                    observed = await _call(
                        tools,
                        "p4rt_exec",
                        args="read-table --switch gateway_1 --table ecn_config",
                    )
                    assert any(
                        r["params"].get("threshold") == 73
                        for r in observed["tables"]["ecn_config"]
                    )
                    assert (
                        await _call(
                            tools,
                            "p4rt_exec",
                            args="insert --switch gateway_1 --table int_watchlist --match srcAddr=192.0.2.11/32 --priority 30 --action NoAction",
                        )
                    )["ok"]
                    assert (
                        await _call(tools, "p4rt_exec", args="apply --switch gateway_1")
                    )["ok"]
                    reset = await _call(
                        tools,
                        "p4rt_exec",
                        args="read-table --switch gateway_1 --table int_watchlist",
                    )
                    entries = [
                        r for r in reset["tables"]["int_watchlist"] if not r["default"]
                    ]
                    assert len(entries) == 1 and entries[0]["priority"] == 1
                    # Leave a new edit for the subsequent fresh native trial to remove.
                    assert (
                        await _call(
                            tools,
                            "p4rt_exec",
                            args="insert --switch leaf_1 --table int_watchlist --priority 40 --action watch_int",
                        )
                    )["ok"]

                asyncio.run(exercise())
            _exec(
                session_id,
                "collector",
                "tail -n 3 /var/lib/int-collector/int_reports.jsonl",
            )
            assert " 0% packet loss" in _exec(
                session_id, "client_1", "ping -c 3 -W 1 10.0.1.11"
            )
            assert "200" in _exec(
                session_id,
                "client_1",
                "curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' http://10.0.1.11",
            )
            assert "200" in _exec(
                session_id,
                "client_1",
                "curl -fsS --max-time 5 -o /dev/null -w '%{http_code}' http://20.0.0.1",
            )
            # Cross-leaf service pairs retain the healthy north-south isolation.
            for source, target in (
                (model.services[0], model.services_on_leaf(model.leaves[1])[0]),
                (model.services_on_leaf(model.leaves[1])[0], model.services[0]),
            ):
                assert "100% packet loss" in _exec(
                    session_id, source.name, f"ping -c 3 -W 1 {target.ip} || true"
                )
                assert "ISOLATED" in _exec(
                    session_id,
                    source.name,
                    f"curl -fsS --max-time 3 -o /dev/null http://{target.ip}; "
                    'code=$?; test "$code" -eq 7 -o "$code" -eq 28 && echo ISOLATED',
                )
            calls = [
                json.loads(line)
                for line in (Path(row["session_dir"]) / "tool_calls.jsonl")
                .read_text()
                .splitlines()
            ]
            assert any(
                c["tool"] == "p4rt_exec"
                and c["arguments"]["args"].startswith("insert ")
                for c in calls
            )
        finally:
            self._close_session(session_id)
        # A new native deployment starts with the authored entries on every switch.
        fresh = self._start_env(
            "p4_dc_gateway", ["-s", "s", "--telemetry", "prometheus"]
        )
        try:
            with mcp_gateway_for_session(fresh, scenario_name="p4_dc_gateway"):

                async def check_reset():
                    tools = await _tools(fresh, "p4_dc_gateway")
                    for switch in model.fabric_switches():
                        result = await _call(
                            tools,
                            "p4rt_exec",
                            args=f"read-table --switch {switch} --table int_watchlist",
                        )
                        entries = [
                            r
                            for r in result["tables"]["int_watchlist"]
                            if not r["default"]
                        ]
                        assert len(entries) == (1 if switch in model.gateways else 0)

                asyncio.run(check_reset())
        finally:
            self._close_session(fresh)

    @pytest.mark.parametrize("profile", ["frr", "nokia_srlinux"])
    def test_router_configuration_through_agent_cli(self, profile):
        extra = ["-s", "s", "--device-profile", profile, "--telemetry", "prometheus"]
        if profile == "nokia_srlinux":
            extra += ["--backend", "containerlab"]
        session_id = self._start_env("dc_clos", extra)
        try:
            with mcp_gateway_for_session(
                session_id,
                scenario_name="dc_clos",
                backend="containerlab" if profile == "nokia_srlinux" else "kathara",
            ):

                async def configure():
                    tools = await _tools(session_id, "dc_clos")
                    if profile == "frr":
                        await _call(
                            tools,
                            "device_read",
                            host_name="spine_1",
                            commands=[
                                "vtysh -c 'configure terminal' -c 'interface lo' -c 'description cliopen' -c 'end'"
                            ],
                        )
                        assert "description cliopen" in str(
                            await _call(
                                tools,
                                "device_read",
                                host_name="spine_1",
                                commands=["vtysh -c 'show running-config'"],
                            )
                        )
                    else:
                        await _call(
                            tools,
                            "srl_exec_cli",
                            device_name="spine_1",
                            command="enter candidate\n/interface ethernet-1/1 description cliopen\ncommit now",
                        )
                        assert "cliopen" in str(
                            await _call(
                                tools,
                                "srl_exec_cli",
                                device_name="spine_1",
                                command="info from running interface ethernet-1/1 description",
                            )
                        )

                asyncio.run(configure())
            command = (
                "vtysh -c 'show running-config'"
                if profile == "frr"
                else "sr_cli 'info from running interface ethernet-1/1 description'"
            )
            assert "cliopen" in _exec(session_id, "spine_1", command)
        finally:
            self._close_session(session_id)
