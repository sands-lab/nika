"""IOS XR removes its uploaded bootstrap configuration after import."""

import asyncio
import json
import time

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.net_env.utils.iosxr.common import IMAGE
from nika.net_env.vendor_validation import _parse_iosxr_bgp_summary
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
    reason="Requires Docker and the IOS XR image",
)
def test_bootstrap_copy_absent_during_live_bgp_fault_and_recovery(tmp_path):
    sid = start_net_env(
        "dc_clos",
        "s",
        device_profile="iosxr",
        telemetry=[],
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
            if "iosxr" in identity.capabilities
        )
        assert len(nodes) >= 2
        target, peer = nodes[:2]
        config = None

        async def state(phase):
            client = MultiServerMCPClient(connections=config)
            tools = {tool.name: tool for tool in await client.get_tools()}

            async def read(node, command):
                args = {"router_name": node, "command": command}
                output = tool_text(await tools["iosxr_exec"].ainvoke(args))
                calls.append(
                    {
                        "phase": phase,
                        "tool": "iosxr_exec",
                        "args": args,
                        "output": output,
                    }
                )
                return output

            observed = {}
            for node in nodes:
                files = await read(node, "dir disk0:")
                assert "startup-config.cfg" not in files
                missing = await read(node, "more disk0:/startup-config.cfg")
                assert "file does not exist" in missing.lower()
                asn, peers = _parse_iosxr_bgp_summary(
                    await read(node, "show bgp summary")
                )
                assert asn is not None and peers
                observed[node] = (
                    asn,
                    frozenset(
                        address for address, peer in peers.items() if peer.established
                    ),
                )
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
            deadline = time.monotonic() + 60
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
            deadline = time.monotonic() + 60
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
