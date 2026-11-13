"""The benchmark engine's HTTP service, its client, and the frontend's gateway use."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

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
    AgentTask,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
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
    tool_call_limits={"diagnosis": 12, "submission": 1},
    access=AgentAccess(role="release", policy=AccessPolicy(node_roles=["host"])),
    sandbox=True,
    bind_host="0.0.0.0",
    agent_host="host.docker.internal",
)


REQUEST = TrialRequest(
    task={"scenario": "dc_clos"},
    failures=["link_down"],
    session_id=HANDLE.session_id,
    trial_dir=HANDLE.trial_dir,
    release="0.3.0",
)
TASK = AgentTask(task_description="Hosts in pod 1 cannot reach the web server.")


def test_engine_launch_preserves_frontend_passwd_identity(
    monkeypatch, tmp_path
) -> None:
    from nika.benchmark_engine import container

    monkeypatch.setattr(container, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(container, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(container, "clab_runtime_root", lambda **_: tmp_path / "clab")
    monkeypatch.setenv("USER", "unrelated-environment-user")
    monkeypatch.setattr(
        container.pwd, "getpwuid", lambda _: SimpleNamespace(pw_name="frontend-user")
    )
    args = container._docker_run_args(
        "image",
        name="engine",
        run_id="run",
        results_root=tmp_path,
        port=1234,
        token="t",
    )
    assert "NIKA_USER=frontend-user" in args


class _StubEngine:
    """Records calls and answers like an engine would."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        # Set to hold start_trial until the test releases it.
        self.release: threading.Event | None = None
        self.deploying = threading.Event()
        self.begin_error: Exception | None = None

    def describe(self, release=None):
        self.calls.append(("describe", release))
        return EngineDescription(
            api_versions=[1],
            release_id="nika-bench",
            release_version=release or "0.3.0",
        )

    def start_trial(self, request):
        self.calls.append(("start_trial", request))
        self.deploying.set()
        if self.release is not None:
            self.release.wait(10)
        return HANDLE

    def begin(self, handle):
        self.calls.append(("begin", handle))
        if self.begin_error is not None:
            raise self.begin_error
        return TASK

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
        if handle.session_id == "broken":
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
    with _serve(create_engine_app(engine, token=TOKEN)) as url:
        client = EngineClient(url, token=TOKEN)
        assert client.describe("0.1.0").release_version == "0.1.0"
        assert client.start_trial(REQUEST) == HANDLE
        # The agent's task comes from the engine, not the shared session store.
        assert client.begin(HANDLE) == TASK
        assert client.agent_endpoints(HANDLE, GATEWAY) == ENDPOINTS
        client.close_agent_endpoints(HANDLE)
        assert client.fault_check(HANDLE).result == "present"
        client.score(HANDLE, result_dir="/results", infra_evidence=True)
        client.stop_trial(HANDLE, status="aborted")

        with pytest.raises(FileNotFoundError):
            client.stop_trial(HANDLE.model_copy(update={"session_id": "gone"}))
        with pytest.raises(EngineError) as failed:
            client.stop_trial(HANDLE.model_copy(update={"session_id": "broken"}))
        assert failed.value.outcome == "infra_failed"
        assert classify_trial_failure(failed.value) == "infra_failed"
        with pytest.raises(EngineError, match="invalid engine token"):
            EngineClient(url, token="wrong").describe()

    assert ("start_trial", REQUEST) in engine.calls
    assert ("begin", HANDLE) in engine.calls
    # The release's access policy reaches the engine that runs the gateway.
    assert ("agent_endpoints", HANDLE, GATEWAY) in engine.calls
    assert ("close_agent_endpoints", HANDLE) in engine.calls
    assert ("score", HANDLE, "/results", True) in engine.calls
    assert ("stop_trial", HANDLE, "aborted") in engine.calls


def test_repeated_requests_do_not_redo_work_and_a_retry_redeploys() -> None:
    engine = _StubEngine()
    engine.begin_error = EngineError("symptoms never appeared", outcome="infra_failed")
    with _serve(create_engine_app(engine, token=TOKEN)) as url:
        client = EngineClient(url, token=TOKEN)
        assert client.start_trial(REQUEST) == HANDLE
        # A start resent after a dropped connection finds the deployed trial.
        assert client.start_trial(REQUEST) == HANDLE
        with pytest.raises(EngineError, match="symptoms never appeared") as failed:
            client.begin(HANDLE)
        assert failed.value.outcome == "infra_failed"
        client.stop_trial(HANDLE, status="error")
        client.stop_trial(HANDLE, status="error")
        # A retried trial reuses its session ID and deploys again.
        assert client.start_trial(REQUEST) == HANDLE

    assert [call[0] for call in engine.calls] == [
        "start_trial",
        "begin",
        "stop_trial",
        "start_trial",
    ]


def test_stop_during_deployment_waits_for_it_then_tears_down() -> None:
    engine = _StubEngine()
    engine.release = threading.Event()
    starting: dict[str, BaseException] = {}

    with _serve(create_engine_app(engine, token=TOKEN)) as url:
        client = EngineClient(url, token=TOKEN)

        def start() -> None:
            try:
                client.start_trial(REQUEST)
            except EngineError as exc:
                starting["error"] = exc

        starter = threading.Thread(target=start)
        starter.start()
        assert engine.deploying.wait(5)
        stopper = threading.Thread(target=client.stop_trial, args=(HANDLE,))
        stopper.start()
        time.sleep(0.3)
        # The stop waits for the deployment instead of racing it.
        assert [call[0] for call in engine.calls] == ["start_trial"]
        engine.release.set()
        stopper.join(10)
        starter.join(10)

    assert [call[0] for call in engine.calls] == ["start_trial", "stop_trial"]
    assert "was stopped" in str(starting["error"])


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


def test_default_gateway_request_retains_published_engine_wire_schema() -> None:
    payload = GatewayRequest().model_dump(mode="json")
    assert "tool_call_limits" not in payload
    assert set(payload) == {"access", "sandbox", "bind_host", "agent_host"}
