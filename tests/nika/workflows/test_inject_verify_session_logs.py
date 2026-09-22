"""E2E: env verify + failure inject write narrative events to nika.jsonl."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nika.mcp.session_context import SESSION_ID_ENV
from nika.utils.session_id import resolve_session_tag
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.support.failure_contract import (
    inject_and_assert_ground_truth,
    resolve_inject_params,
)
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.e2e,
]


def _load_events(session_dir: Path) -> list[dict]:
    path = session_dir / "nika.jsonl"
    assert path.is_file(), f"missing nika.jsonl under {session_dir}"
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _events_of(rows: list[dict], event: str) -> list[dict]:
    return [row for row in rows if row.get("event") == event]


def test_dc_clos_link_down_session_logs_narrate_verify_and_change() -> None:
    if not docker_available():
        pytest.skip("Docker not available")

    scenario = "dc_clos"
    problem = "link_down"
    inject_params = resolve_inject_params(scenario, problem, topo_size="s")
    session_id = start_net_env(
        scenario,
        "s",
        session_tag=resolve_session_tag(context="test"),
        backend="kathara",
    )
    prev = os.environ.get(SESSION_ID_ENV)
    os.environ[SESSION_ID_ENV] = session_id
    try:
        inject_and_assert_ground_truth(session_id, scenario, problem, inject_params)
        session_dir = Path(SessionStore().get_session(session_id)["session_dir"])
        rows = _load_events(session_dir)

        env_verify = _events_of(rows, "env_verify")
        assert env_verify, "expected env_verify event"
        env_msg = env_verify[-1].get("message") or ""
        env_data = env_verify[-1].get("data") or {}
        assert "passed" in env_msg or "ok=" in env_msg
        assert env_data.get("checks"), "env_verify should include checks"
        assert env_data.get("details"), "env_verify should include probe details"
        assert "client_0" in json.dumps(env_data["details"])

        injected = _events_of(rows, "failure_injected")
        assert injected, "expected failure_injected event"
        inj_msg = injected[-1].get("message") or ""
        inj_data = injected[-1].get("data") or {}
        assert "link_down" in inj_msg
        assert "host_name=" in inj_msg or inj_data.get("resolved_params")
        assert inj_data.get("injection_params") or inj_data.get("resolved_params")

        verified = _events_of(rows, "failure_verified")
        assert verified, "expected failure_verified event"
        ver_msg = verified[-1].get("message") or ""
        assert "details=" in ver_msg or "operstate" in ver_msg

        changes = _events_of(rows, "network_change")
        assert changes, "expected network_change from link operstate apply"
        assert any(
            (row.get("data") or {}).get("mechanism") == "link_operstate"
            or "link down" in (row.get("message") or "")
            for row in changes
        )
    finally:
        close_session(session_id=session_id)
        if prev is None:
            os.environ.pop(SESSION_ID_ENV, None)
        else:
            os.environ[SESSION_ID_ENV] = prev
