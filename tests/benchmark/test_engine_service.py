"""The benchmark engine's HTTP service speaks engine API version 1."""

from __future__ import annotations

import threading
import time

import pytest
from starlette.testclient import TestClient

from nika.benchmark_engine.protocol import (
    AgentEndpoints,
    AgentTask,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
    Resource,
    Scores,
    TaskInfo,
    TaskList,
    TrialHandle,
    TrialRequest,
)
from nika.benchmark_engine.server import create_engine_app

TOKEN = "engine-token"
HEADERS = {"NIKA-Engine-Token": TOKEN}
HANDLE = TrialHandle(
    session_id="dc_clos_bgp__link_down__0a1b2c3d__t01__r01234567",
    trial_dir="/results/trials/dc_clos_bgp__link_down__0a1b2c3d__t01",
    resources=[
        Resource(
            kind="nika_session",
            id="dc_clos_bgp__link_down__0a1b2c3d__t01__r01234567",
        )
    ],
)
REQUEST = TrialRequest(
    task_id="dc_clos_bgp__link_down__0a1b2c3d",
    session_id=HANDLE.session_id,
    trial_dir=HANDLE.trial_dir,
    release="0.1.0",
    split="test",
)


class _StubEngine:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.release = threading.Event()
        self.release.set()
        self.begin_error: Exception | None = None

    def describe(self, release=None):
        self.calls.append(("describe", release))
        return EngineDescription(
            api_versions=[1], release_id="nika-bench", release_version="0.1.0"
        )

    def list_tasks(self, release, split):
        self.calls.append(("list_tasks", release, split))
        return TaskList(
            release=release,
            split=split,
            task_file_sha256="0" * 64,
            tasks=[
                TaskInfo(
                    task_id=REQUEST.task_id,
                    scenario="dc_clos_bgp",
                    topo_size="s",
                    resource_class="light",
                    faults=["link_down"],
                    label="dc_clos_bgp/link_down s host=leaf_router_0_0",
                )
            ],
        )

    def start_trial(self, request):
        self.calls.append(("start_trial", request.task_id))
        self.release.wait(10)
        return HANDLE

    def begin(self, handle):
        self.calls.append(("begin", handle.session_id))
        if self.begin_error is not None:
            raise self.begin_error
        return AgentTask(task_description="Hosts cannot reach the web server.")

    def agent_endpoints(self, handle, request):
        self.calls.append(("agent_endpoints", request.sandbox))
        return AgentEndpoints(
            gateway_url="http://127.0.0.1:9100",
            agent_url="http://127.0.0.1:9100",
            session_token="session-secret",
            phase_token="phase-secret",
            servers=["kathara_base_mcp_server", "task_mcp_server"],
        )

    def close_agent_endpoints(self, handle):
        self.calls.append(("close_agent_endpoints", handle.session_id))

    def fault_check(self, handle):
        return FaultCheck(result="error", error="no recheck")

    def score(self, handle, *, infra_evidence=False):
        self.calls.append(("score", infra_evidence))
        return Scores(
            status="scored", scoring_id="rule-based-v2", metrics={"rca_f1": 1.0}
        )

    def stop_trial(self, handle, *, status="finished"):
        self.calls.append(("stop_trial", handle.session_id, status))


def _post(client, operation, body):
    return client.post(f"/v1/{operation}", json=body, headers=HEADERS)


def _wait_state(client, session_id, states, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _post(client, "trial_status", {"session_id": session_id}).json()
        if status["result"]["state"] in states:
            return status["result"]
        time.sleep(0.02)
    raise AssertionError(f"trial never reached {states}")


@pytest.fixture
def engine():
    return _StubEngine()


@pytest.fixture
def client(engine):
    with TestClient(create_engine_app(engine, token=TOKEN)) as test_client:
        yield test_client


def test_health_needs_no_token_and_reports_api(client) -> None:
    assert client.get("/health").json() == {"status": "ok", "api": 1}


def test_operations_need_the_run_token(client) -> None:
    response = client.post("/v1/describe", json={"release": "0.1.0"})
    assert response.status_code == 401
    wrong = client.post(
        "/v1/describe", json={}, headers={"NIKA-Engine-Token": "other"}
    )
    assert wrong.status_code == 401


def test_unknown_operation_is_404(client) -> None:
    assert _post(client, "schedule_operation", {}).status_code == 404


def test_describe_and_list_tasks_reply_in_result(client, engine) -> None:
    described = _post(client, "describe", {"release": "0.1.0"}).json()["result"]
    assert EngineDescription.model_validate(described).release_version == "0.1.0"
    listed = _post(client, "list_tasks", {"release": "0.1.0", "split": "test"})
    tasks = TaskList.model_validate(listed.json()["result"])
    assert tasks.tasks[0].task_id == REQUEST.task_id
    assert ("list_tasks", "0.1.0", "test") in engine.calls


def test_trial_lifecycle_runs_in_the_background(client, engine) -> None:
    engine.release.clear()
    started = _post(client, "start_trial", {"request": REQUEST.model_dump()})
    assert started.json()["result"]["state"] == "deploying"
    # Repeating start_trial returns the current state instead of redeploying.
    again = _post(client, "start_trial", {"request": REQUEST.model_dump()})
    assert again.json()["result"]["state"] == "deploying"
    engine.release.set()
    deployed = _wait_state(client, HANDLE.session_id, {"deployed"})
    assert deployed["handle"]["session_id"] == HANDLE.session_id
    assert [c for c in engine.calls if c[0] == "start_trial"] == [
        ("start_trial", REQUEST.task_id)
    ]

    _post(client, "begin", {"handle": HANDLE.model_dump()})
    ready = _wait_state(client, HANDLE.session_id, {"ready"})
    assert ready["task"]["task_description"].startswith("Hosts")

    endpoints = _post(
        client,
        "agent_endpoints",
        {"handle": HANDLE.model_dump(), "request": GatewayRequest().model_dump()},
    ).json()["result"]
    assert AgentEndpoints.model_validate(endpoints).phase_token == "phase-secret"

    scores = _post(
        client, "score", {"handle": HANDLE.model_dump(), "infra_evidence": True}
    ).json()["result"]
    assert Scores.model_validate(scores).metrics == {"rca_f1": 1.0}
    assert ("score", True) in engine.calls

    _post(client, "stop_trial", {"handle": HANDLE.model_dump(), "status": "aborted"})
    _wait_state(client, HANDLE.session_id, {"stopped"})
    assert ("stop_trial", HANDLE.session_id, "aborted") in engine.calls


def test_failed_begin_reports_its_outcome(client, engine) -> None:
    engine.begin_error = EngineError("inject failed", outcome="endpoint_failed")
    _post(client, "start_trial", {"request": REQUEST.model_dump()})
    _wait_state(client, HANDLE.session_id, {"deployed"})
    _post(client, "begin", {"handle": HANDLE.model_dump()})
    failed = _wait_state(client, HANDLE.session_id, {"failed"})
    assert failed["outcome"] == "endpoint_failed"
    assert failed["error"] == "inject failed"


def test_engine_refuses_task_rows(client, engine) -> None:
    body = {
        "request": {
            "task": {"scenario": "dc_clos_bgp", "problem": "link_down"},
            "session_id": "s1",
            "trial_dir": "/results/trials/s1",
        }
    }
    response = _post(client, "start_trial", body)
    assert response.status_code == 500
    assert response.json()["outcome"] == "infra_failed"
    assert "task_id" in response.json()["error"]
    assert not [c for c in engine.calls if c[0] == "start_trial"]


def test_stop_of_a_trial_this_service_never_started(client, engine) -> None:
    _post(client, "stop_trial", {"handle": HANDLE.model_dump()})
    _wait_state(client, HANDLE.session_id, {"stopped"})
    assert ("stop_trial", HANDLE.session_id, "finished") in engine.calls


def test_unknown_trial_status_is_file_not_found(client) -> None:
    response = _post(client, "trial_status", {"session_id": "missing"})
    assert response.status_code == 500
    assert response.json()["error_type"] == "FileNotFoundError"


def test_frontend_request_fields_validate() -> None:
    """The frontend sends these optional fields only when they are set."""
    request = GatewayRequest.model_validate(
        {"sandbox": False, "bind_host": None, "agent_host": "127.0.0.1"}
    )
    assert request.tool_call_limits is None
    assert "tool_call_limits" not in request.model_dump()
