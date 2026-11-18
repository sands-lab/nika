"""Fault-presence gates follow actual scheduled activation and recovery."""

from __future__ import annotations

import json

import pytest

from nika.benchmark_engine import use_engine
from nika.benchmark_engine.engine import BenchmarkEngine
from nika.benchmark_engine.protocol import TrialRequest
from nika.evaluator.validity import detect_trial_validity
from nika.runtime.factory import runtime_for_session
from nika.utils.session_store import SessionStore
from nika.validation.presence import PresenceWatch
from tests.nika.workflows.test_schedule_live import _events, _exec, _wait
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]


@pytest.mark.parametrize("persistent_peer", [False, True])
def test_scheduled_recovery_preserves_other_fault_presence(tmp_path, persistent_peer):
    events = [
        {
            "problem": "link_down",
            "inject": {"host_name": "pc1", "intf_name": "eth0"},
            "duration": 10,
            "repeat": {"every": 16, "count": 2},
        }
    ]
    if persistent_peer:
        events.append(
            {
                "problem": "link_down",
                "inject": {"host_name": "pc2", "intf_name": "eth0"},
            }
        )
    engine = BenchmarkEngine()
    handle = engine.start_trial(
        TrialRequest(
            task={
                "scenario": "simple_bgp",
                "problem": "link_down",
                "schedule": {"agent_start": 0, "events": events},
            },
            failures=["link_down"],
            result_dir=str(tmp_path),
        )
    )
    runtime = runtime_for_session(SessionStore().get_session(handle.session_id))
    try:
        with use_engine(engine):
            engine.begin(handle)
            engine.schedule_operation(handle, "start")
            engine.schedule_operation(handle, "agent-ready")

            def recorded(status, occurrence):
                return any(
                    row["status"] == status
                    and row.get("event_index") == 0
                    and row.get("occurrence_index") == occurrence
                    for row in _events(handle.trial_path)
                )

            _wait(lambda: recorded("verified", 0))
            assert engine.fault_check(handle).result == "present"
            _wait(lambda: recorded("recovered", 0))
            assert (
                "UP"
                in _exec(
                    handle.session_id, "pc1", "cat /sys/class/net/eth0/operstate"
                ).upper()
            )
            assert engine.fault_check(handle).result == (
                "present" if persistent_peer else "not_applicable"
            )
            PresenceWatch(handle.session_id, handle.trial_path).finish()
            assert (
                "fault_absent_at_cleanup"
                not in detect_trial_validity(handle.trial_path).flags
            )

            _wait(lambda: recorded("verified", 1))
            check = engine.fault_check(handle)
            assert check.result == "present"
            assert any(
                row["occurrence_index"] == 1
                for row in check.evidence["active_occurrences"]
            )
            _exec(handle.session_id, "pc1", "ip link set eth0 up")
            assert engine.fault_check(handle).result == "absent"
            _exec(handle.session_id, "pc1", "ip link set eth0 down")
            _wait(lambda: recorded("recovered", 1))
            assert engine.fault_check(handle).result == (
                "present" if persistent_peer else "not_applicable"
            )
            assert engine.schedule_operation(handle, "status")["error"] is None
            checks = [
                json.loads(line)["data"]
                for line in (handle.trial_path / "nika.jsonl").read_text().splitlines()
                if json.loads(line).get("event") == "fault_presence_recheck"
            ]
            assert checks and all(row["result"] != "absent" for row in checks)
    finally:
        engine.stop_trial(handle)
        assert not runtime.exists()
        assert not runtime.has_leftover_resources()
