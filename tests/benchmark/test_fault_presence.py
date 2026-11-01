"""Lightweight artifact rechecks logged during a benchmark trial."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.validation import presence
from nika.validation.presence import (
    PresenceWatch,
    bind_injected_problem,
    clear_injected_problem,
    record_injection_verify,
)

pytestmark = pytest.mark.unit


class _ArtifactFault:
    def __init__(self, *, present: bool = True, asn: int | None = 65000) -> None:
        self.root_cause_name = "bgp_asn_misconfig"
        self._wrong_asn = asn
        self.present = present
        self.calls: list[object] = []

    def recheck_artifact(self, params: object = None) -> dict:
        self.calls.append(params)
        ok = self.present and self._wrong_asn == 65000
        return {
            "present": ok,
            "fault": self.root_cause_name,
            "scope": "artifact",
            "evidence": {"wrong_asn": self._wrong_asn},
            "error": None if ok else "fault artifact absent",
        }


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    checks: list[dict] = []
    monkeypatch.setattr(presence, "log_presence_check", checks.append)
    return checks


def test_fast_agent_still_records_during_and_before(
    tmp_path: Path, logged: list[dict]
) -> None:
    fault = _ArtifactFault()
    bind_injected_problem("s-fast", fault, {"host": "r1"})
    record_injection_verify(
        fault="bgp_asn_misconfig",
        verify_result={"verified": True, "details": {"running_asn": 65000}},
    )
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    watch = PresenceWatch("s-fast", tmp_path, delay_sec=30)
    watch.start()
    watch.finish()
    clear_injected_problem("s-fast")
    assert [item["phase"] for item in logged] == [
        "post_inject",
        "during_agent",
        "before_cleanup",
    ]
    assert all(item["result"] == "present" for item in logged[1:])
    assert logged[0]["check"] == "verify_fault"


def test_absent_artifact_is_only_logged(tmp_path: Path, logged: list[dict]) -> None:
    fault = _ArtifactFault(present=False)
    bind_injected_problem("s-absent", fault, None)
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    watch = PresenceWatch("s-absent", tmp_path, delay_sec=30)
    watch.start()
    assert watch.finish() is None
    clear_injected_problem("s-absent")
    assert [item["result"] for item in logged] == ["absent", "absent"]
    assert all(item["error"] == "fault artifact absent" for item in logged)


def test_remote_artifact_is_checked_during_and_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logged: list[dict]
) -> None:
    from nika.remote.client import RemoteClient

    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    from nika.benchmark_engine import engine as engine_module

    monkeypatch.setattr(engine_module, "remote_lab", lambda: True)
    calls: list[str] = []

    def remote_check(_self: RemoteClient, session_id: str) -> dict:
        calls.append(session_id)
        return {
            "present": len(calls) == 1,
            "fault": "bgp_asn_misconfig",
            "evidence": {},
            "error": None if len(calls) == 1 else "fault artifact absent",
        }

    monkeypatch.setattr(RemoteClient, "fault_artifact", remote_check)
    watch = PresenceWatch("s-remote", tmp_path, delay_sec=30)
    watch.start()
    watch.finish()
    assert len(calls) == 3
    assert [item["phase"] for item in logged] == [
        "post_inject",
        "during_agent",
        "before_cleanup",
    ]
    assert logged[-1]["result"] == "absent"


def test_recheck_uses_the_injected_instance() -> None:
    injected = _ArtifactFault(asn=65000)
    other = _ArtifactFault(asn=None)
    bind_injected_problem("s-instance", injected, {"host": "r1"})
    raw_calls_before = len(other.calls)
    from nika.validation.presence import recheck_bound_artifact

    result = recheck_bound_artifact("s-instance")
    clear_injected_problem("s-instance")
    assert result["present"] is True
    assert result["evidence"]["wrong_asn"] == 65000
    assert other.calls == [] or len(other.calls) == raw_calls_before
    assert injected.calls


def test_recheck_retries_once_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(presence.time, "sleep", lambda _sec: None)

    class _TimeoutThenPresent:
        root_cause_name = "arp_cache_poisoning"

        def __init__(self) -> None:
            self.calls = 0

        def recheck_artifact(self, params: object = None) -> dict:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Timed out running `ip -j route` on client_10_1")
            return {
                "present": True,
                "fault": self.root_cause_name,
                "scope": "artifact",
                "evidence": {},
                "error": None,
            }

    fault = _TimeoutThenPresent()
    bind_injected_problem("s-timeout", fault, {"host_name": "client_10_1"})
    from nika.validation.presence import recheck_bound_artifact

    result = recheck_bound_artifact("s-timeout")
    clear_injected_problem("s-timeout")
    assert result["present"] is True
    assert fault.calls == 2
    assert result["error"] is None
