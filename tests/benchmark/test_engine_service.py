"""The benchmark engine's HTTP service, its client, and the frontend's gateway use."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import uvicorn

from nika.agents.mcp_config import engine_gateway, select_session_servers
from nika.agents.sandbox.config import ENV_GATEWAY_SERVERS, ENV_GATEWAY_URL
from nika.benchmark_engine import use_engine
from nika.benchmark_engine.client import EngineClient
from nika.benchmark_engine.protocol import (
    AccessPolicy,
    AgentAccess,
    AgentEndpoints,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
    InjectResult,
    Resource,
    TrialHandle,
    TrialRequest,
)
from nika.benchmark_engine.server import create_engine_app
from nika.utils.net import pick_free_port
from nika.workflows.benchmark.outcomes import classify_trial_failure

pytestmark = pytest.mark.unit

TOKEN = "engine-token"
HANDLE = TrialHandle(
    session_id="trial-1",
    trial_dir="/results/trials/t1",
    resources=[Resource(kind="nika_session", id="trial-1")],
)


ENDPOINTS = AgentEndpoints(
    gateway_url="http://127.0.0.1:9100",
    agent_url="http://host.docker.internal:9100",
    session_token="session-secret",
    phase_token="phase-secret",
    servers=["kathara_base_mcp_server", "task_mcp_server"],
)
GATEWAY = GatewayRequest(
    access=AgentAccess(role="release", policy=AccessPolicy(node_roles=["host"])),
    sandbox=True,
    bind_host="0.0.0.0",
    agent_host="host.docker.internal",
)


class _StubEngine:
    """Records calls and answers like an engine would."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def describe(self, release=None):
        self.calls.append(("describe", release))
        return EngineDescription(
            api_versions=[1],
            release_id="nika-bench",
            release_version=release or "0.2.0",
        )

    def start_trial(self, request):
        self.calls.append(("start_trial", request))
        return HANDLE

    def inject(self, handle, request):
        self.calls.append(("inject", handle, request))
        return InjectResult(ground_truth=True)

    def agent_endpoints(self, handle, request):
        self.calls.append(("agent_endpoints", handle, request))
        return ENDPOINTS

    def close_agent_endpoints(self, handle):
        self.calls.append(("close_agent_endpoints", handle))

    def fault_check(self, handle):
        return FaultCheck(result="present", fault="link_down")

    def score(self, handle, *, result_dir=None, infra_evidence=False):
        self.calls.append(("score", handle, result_dir, infra_evidence))

    def stop_trial(self, handle, *, status="finished"):
        if handle.session_id == "gone":
            raise FileNotFoundError("Session 'gone' not found")
        if status == "error":
            raise EngineError("undeploy failed", outcome="infra_failed")
        self.calls.append(("stop_trial", handle, status))


@contextmanager
def _serve(app) -> Iterator[str]:
    port = pick_free_port("127.0.0.1")
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_client_drives_every_operation_over_http() -> None:
    engine = _StubEngine()
    request = TrialRequest(task={"scenario": "dc_clos"}, failures=["link_down"])
    with _serve(create_engine_app(engine, token=TOKEN)) as url:
        client = EngineClient(url, token=TOKEN)
        assert client.describe("0.1.0").release_version == "0.1.0"
        assert client.start_trial(request) == HANDLE
        assert client.inject(HANDLE, request).ground_truth is True
        assert client.agent_endpoints(HANDLE, GATEWAY) == ENDPOINTS
        client.close_agent_endpoints(HANDLE)
        assert client.fault_check(HANDLE).result == "present"
        client.score(HANDLE, result_dir="/results", infra_evidence=True)
        client.stop_trial(HANDLE, status="aborted")

        with pytest.raises(FileNotFoundError):
            client.stop_trial(HANDLE.model_copy(update={"session_id": "gone"}))
        with pytest.raises(EngineError) as failed:
            client.stop_trial(HANDLE, status="error")
        assert failed.value.outcome == "infra_failed"
        assert classify_trial_failure(failed.value) == "infra_failed"
        with pytest.raises(EngineError, match="invalid engine token"):
            EngineClient(url, token="wrong").describe()

    assert ("start_trial", request) in engine.calls
    # The release's access policy reaches the engine that runs the gateway.
    assert ("agent_endpoints", HANDLE, GATEWAY) in engine.calls
    assert ("close_agent_endpoints", HANDLE) in engine.calls
    assert ("score", HANDLE, "/results", True) in engine.calls
    assert ("stop_trial", HANDLE, "aborted") in engine.calls


def test_engine_gateway_exports_the_engine_gateway_for_one_agent_run(
    monkeypatch,
) -> None:
    monkeypatch.delenv(ENV_GATEWAY_SERVERS, raising=False)
    monkeypatch.setenv(ENV_GATEWAY_URL, "http://previous:1")
    engine = _StubEngine()
    with use_engine(engine):
        with engine_gateway(HANDLE.session_id, HANDLE.trial_dir, GATEWAY) as endpoints:
            assert endpoints == ENDPOINTS
            assert os.environ[ENV_GATEWAY_URL] == ENDPOINTS.gateway_url
            # The agent mounts what the release's engine serves, even servers
            # this NIKA's registry does not know.
            assert select_session_servers("unknown_scenario") == ENDPOINTS.servers

    assert os.environ[ENV_GATEWAY_URL] == "http://previous:1"
    assert ENV_GATEWAY_SERVERS not in os.environ
    assert engine.calls == [
        ("agent_endpoints", HANDLE.model_copy(update={"resources": []}), GATEWAY),
        ("close_agent_endpoints", HANDLE.model_copy(update={"resources": []})),
    ]
