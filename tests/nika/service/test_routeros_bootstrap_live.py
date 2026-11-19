"""RouterOS exposes live state without retaining its bootstrap configuration."""

import asyncio
import json
import re
import time

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.net_env.utils.routeros.common import IMAGE
from nika.net_env.utils.routeros.parse import routeros_terse_rows
from nika.problems.rca.inventory import load_session_offline_net_env
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.session_store import SessionStore
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available, docker_image_available

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not docker_available() or not docker_image_available(IMAGE),
    reason="Requires Docker and the RouterOS image",
)
def test_bootstrap_copy_absent_during_live_bgp_fault_and_recovery(tmp_path):
    sid = start_net_env(
        "dc_clos",
        "s",
        device_profile="routeros",
        telemetry=["netflow", "prometheus", "syslog"],
        traffic={"pattern": "uniform", "load_mbps": 1.0},
        session_tag=resolve_session_tag(context="test"),
    )
    calls = []
    failure = None
    runtime = None
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)
        metadata = SessionStore().get_session(sid)
        env = load_session_offline_net_env(metadata)
        nodes = sorted(
            node
            for node, identity in env.machine_identities.items()
            if "routeros" in identity.capabilities
        )
        assert len(nodes) >= 2
        target, peer = nodes[:2]
        config = None
        protocol_start = time.time()

        async def state(phase):
            client = MultiServerMCPClient(connections=config)
            tools = {tool.name: tool for tool in await client.get_tools()}

            async def call(tool, args):
                output = tool_text(await tools[tool].ainvoke(args))
                calls.append(
                    {"phase": phase, "tool": tool, "args": args, "output": output}
                )
                return output

            async def read(node, command):
                return await call(
                    "routeros_exec", {"router_name": node, "command": command}
                )

            observed = {}
            missing_targets = []
            configuration_logs = []
            for node in nodes:
                files = await read(node, "/file/print detail")
                assert "config.auto.log" in files
                assert "config.auto.rsc" not in files
                receipt = await read(node, ":put [/file get config.auto.log contents]")
                assert "Script file loaded and executed successfully" in receipt
                missing = await read(node, ":put [/file get config.auto.rsc contents]")
                assert "no such item" in missing.lower()
                instances = routeros_terse_rows(
                    await read(node, "/routing/bgp/instance/print terse")
                )
                assert len(instances) == 1
                sessions = routeros_terse_rows(
                    await read(
                        node, "/routing/bgp/session/print terse where established=yes"
                    )
                )
                observed[node] = (
                    int(instances[0]["as"]),
                    frozenset(row["remote.address"] for row in sessions),
                )
                targets = await read(node, "/ip/traffic-flow/target/print count-only")
                if not re.fullmatch(r"\s*[1-9][0-9]*\s*", targets):
                    missing_targets.append((node, targets))
                await read(node, "/system/logging/print detail")
                history = await read(
                    node,
                    '/log/print without-paging where topics~"system" and message~"routing bgp"',
                )
                if re.search(
                    r"/routing bgp (?:instance|connection) (?:add|set|remove)\b",
                    history,
                ):
                    configuration_logs.append((node, history))
                protocol = await read(
                    node, '/log/print without-paging where topics~"bgp"'
                )
                assert "route,bgp" in protocol, (
                    "Native BGP protocol events must remain observable"
                )
            assert not missing_targets, (
                f"Missing native flow targets: {missing_targets}"
            )
            assert not configuration_logs, (
                f"Readable original or changed BGP commands: {configuration_logs}"
            )
            for node in nodes:
                args = {
                    "start_time": "-600",
                    "host": node,
                    "contains": "/routing bgp",
                    "limit": 1,
                }
                text = await call("syslog_query", args)
                assert not text.strip(), "Syslog retains BGP configuration commands"
            if phase == "recovered":
                deadline = time.monotonic() + 60
                while True:
                    args = {
                        "start_time": str(protocol_start),
                        "host": target,
                        "contains": "Established",
                        "limit": 1,
                    }
                    text = await call("syslog_query", args)
                    events = json.loads(text) if text.strip() else []
                    events = [events] if isinstance(events, dict) else events
                    if any(
                        "Established" in event.get("message", "") for event in events
                    ):
                        break
                    assert time.monotonic() < deadline, (
                        "The fresh BGP recovery event did not reach syslog"
                    )
                    await asyncio.sleep(2)
            if phase == "healthy":
                deadline = time.monotonic() + 120
                while True:
                    coverage = json.loads(await call("flow_coverage", {"limit": 100}))
                    received = {
                        row["node"]
                        for row in coverage["rows"]
                        if row.get("newest_record") and row.get("last_update")
                    }
                    if set(nodes) <= received:
                        break
                    assert time.monotonic() < deadline, (
                        f"Flow records are missing from {set(nodes) - received}"
                    )
                    await asyncio.sleep(2)
            return observed

        with engine_gateway(sid, session.session_dir, GatewayRequest()):
            config = load_session_mcp_config(sid, "dc_clos", phase="diagnosis")
            baseline = asyncio.run(state("healthy"))
            assert baseline[target][1] and baseline[peer][1]
            failure = prepare_failure(
                session, ["bgp_asn_misconfig"], {"host_name": target}
            )
            failure.inject()
            assert failure.verify()["verified"]
            deadline = time.monotonic() + 30
            while True:
                injected = asyncio.run(state("injected"))
                if not injected[target][1]:
                    break
                assert time.monotonic() < deadline, (
                    "Mismatched-AS sessions remain Established"
                )
                time.sleep(1)
            assert injected[target][0] != baseline[target][0]
            assert injected[peer] == baseline[peer]
            assert failure.recover()["verified"]
            failure = None
            deadline = time.monotonic() + 30
            while True:
                recovered = asyncio.run(state("recovered"))
                if recovered == baseline:
                    break
                assert time.monotonic() < deadline, (
                    "BGP did not recover its original peers"
                )
                time.sleep(1)
    finally:
        (tmp_path / "gateway-calls.json").write_text(json.dumps(calls, indent=2) + "\n")
        try:
            if failure is not None:
                failure.recover()
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
