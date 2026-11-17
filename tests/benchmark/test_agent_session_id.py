"""Agents see an opaque session ID, never the trial's, which names its task."""

from __future__ import annotations

import re

import pytest

from nika.benchmark_engine.engine import BenchmarkEngine
from nika.benchmark_engine.protocol import GatewayRequest, TrialHandle, TrialRequest
from nika.utils.session import Session
from nika.utils.session_store import SessionStore

TASK_ID = "dc_clos_bgp__host_crash__45fc915a"
AGENT_ID_RE = re.compile(r"^\d{8}-\d{6}-a-[0-9a-f]{6}$")


@pytest.fixture
def started_trial(tmp_path, monkeypatch):
    """A trial started by the engine, with the deploy replaced by a session."""
    from nika.workflows.env import start

    session_id = f"{TASK_ID}__t01__rtest{tmp_path.name[-6:]}"
    trial_dir = tmp_path / "trials" / f"{TASK_ID}__t01"

    def fake_start_net_env(scenario, size, **kwargs):
        Session().init_session(
            session_id=kwargs["session_id"],
            scenario_name=scenario,
            lab_name=f"{scenario}__test",
            scenario_topo_size=size,
            session_dir=kwargs["session_dir"],
        )
        return kwargs["session_id"]

    monkeypatch.setattr(start, "start_net_env", fake_start_net_env)
    engine = BenchmarkEngine()
    handle = engine.start_trial(
        TrialRequest(
            task_id=TASK_ID,
            release="0.1.0",
            split="test",
            session_id=session_id,
            trial_dir=str(trial_dir),
        )
    )
    yield engine, handle
    engine.close_agent_endpoints(handle)
    try:
        SessionStore().delete_session(session_id)
    except FileNotFoundError:
        pass
    SessionStore().index.purge(session_id)


def test_start_trial_records_an_opaque_agent_session_id(started_trial) -> None:
    _, handle = started_trial
    record = SessionStore().get_session(handle.session_id)
    agent_id = record["agent_session_id"]
    assert AGENT_ID_RE.match(agent_id)
    for part in TASK_ID.split("__"):
        assert part not in agent_id


def test_agent_endpoints_do_not_reveal_the_trial(started_trial) -> None:
    engine, handle = started_trial
    endpoints = engine.agent_endpoints(handle, GatewayRequest())
    exposed = endpoints.model_dump_json()
    assert handle.session_id not in exposed
    assert TASK_ID not in exposed
    from nika.service.mcp_gateway.session_registry import get_session, is_withheld

    agent_id = SessionStore().get_session(handle.session_id)["agent_session_id"]
    assert get_session(agent_id).session_id == handle.session_id
    assert get_session(handle.session_id) is None
    assert is_withheld(handle.session_id)


def test_each_trial_gets_its_own_agent_session_id(tmp_path) -> None:
    from nika.utils.agent_session_id import make_agent_session_id

    assert len({make_agent_session_id() for _ in range(50)}) == 50


def test_handle_without_a_record_cannot_open_endpoints() -> None:
    handle = TrialHandle(session_id="missing", trial_dir="/results/trials/missing")
    with pytest.raises(FileNotFoundError):
        BenchmarkEngine().agent_endpoints(handle, GatewayRequest())
