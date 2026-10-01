"""Lightweight artifact rechecks and environment-invalid trial outcomes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.problems.base import ProblemBase
from nika.validation.presence import (
    PRESENCE_FILENAME,
    EnvironmentInvalid,
    PresenceWatch,
    bind_injected_problem,
    clear_injected_problem,
    raise_if_presence_failed,
    record_injection_verify,
)
from nika.workflows.benchmark.outcomes import (
    ENVIRONMENT_INVALID,
    classify_trial_failure,
)
from nika.workflows.benchmark.run import _finalize_post_inject_failure

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


def _phases(root: Path) -> list[str]:
    payload = json.loads((root / PRESENCE_FILENAME).read_text(encoding="utf-8"))
    return [item["phase"] for item in payload["checks"]]


def test_fast_agent_still_records_during_and_before(tmp_path: Path) -> None:
    fault = _ArtifactFault()
    bind_injected_problem("s-fast", fault, {"host": "r1"})
    record_injection_verify(
        tmp_path,
        fault="bgp_asn_misconfig",
        verify_result={"verified": True, "details": {"running_asn": 65000}},
    )
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    watch = PresenceWatch("s-fast", tmp_path, delay_sec=30)
    watch.start()
    failure = watch.finish()
    clear_injected_problem("s-fast")
    assert failure is None
    assert _phases(tmp_path) == ["post_inject", "during_agent", "before_cleanup"]
    assert all(item["result"] == "present" for item in _checks(tmp_path)[1:])
    assert _checks(tmp_path)[0]["check"] == "verify_fault"
    assert "network effect" not in (tmp_path / PRESENCE_FILENAME).read_text()


def test_normal_window_checks_once_during_agent(tmp_path: Path) -> None:
    fault = _ArtifactFault()
    bind_injected_problem("s-normal", fault, {"host": "r1"})
    record_injection_verify(
        tmp_path,
        fault="bgp_asn_misconfig",
        verify_result={"verified": True, "details": {}},
    )
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    watch = PresenceWatch("s-normal", tmp_path, delay_sec=0)
    watch.start()
    deadline = 0
    while "during_agent" not in _phases(tmp_path) and deadline < 50:
        deadline += 1
        import time

        time.sleep(0.02)
    failure = watch.finish()
    clear_injected_problem("s-normal")
    assert failure is None
    assert _phases(tmp_path).count("during_agent") == 1
    assert _phases(tmp_path)[-1] == "before_cleanup"


def test_absent_artifact_is_environment_invalid(tmp_path: Path) -> None:
    fault = _ArtifactFault(present=False)
    bind_injected_problem("s-absent", fault, None)
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    watch = PresenceWatch("s-absent", tmp_path, delay_sec=30)
    watch.start()
    failure = watch.finish()
    clear_injected_problem("s-absent")
    assert failure is not None
    with pytest.raises(EnvironmentInvalid):
        raise_if_presence_failed(failure, RuntimeError("agent finished early"))
    try:
        raise_if_presence_failed(failure, None)
    except EnvironmentInvalid as exc:
        assert classify_trial_failure(exc) == ENVIRONMENT_INVALID


def test_remote_artifact_is_checked_during_and_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nika.remote.client import RemoteClient
    from nika.validation import presence

    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    monkeypatch.setattr(presence, "_remote_lab", lambda: True)
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
    failure = watch.finish()
    assert len(calls) == 3
    assert failure is not None and "before_cleanup" in failure
    assert _phases(tmp_path) == ["post_inject", "during_agent", "before_cleanup"]


def test_timeout_and_keyboard_interrupt_paths() -> None:
    with pytest.raises(SystemExit):
        raise_if_presence_failed(None, SystemExit(143))
    with pytest.raises(EnvironmentInvalid):
        raise_if_presence_failed("during_agent: fault artifact absent", SystemExit(143))
    with pytest.raises(KeyboardInterrupt):
        raise_if_presence_failed(
            "during_agent: fault artifact absent", KeyboardInterrupt()
        )


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


def test_recheck_retries_a_transient_runtime_timeout(monkeypatch) -> None:
    from nika.validation import presence

    monkeypatch.setattr(presence.time, "sleep", lambda _seconds: None)

    class _IntermittentFault:
        root_cause_name = "mac_address_conflict"

        def __init__(self) -> None:
            self.calls = 0

        def recheck_artifact(self, _params=None) -> dict:
            self.calls += 1
            return {
                "present": self.calls == 2,
                "evidence": {"mac": "[TIMEOUT]" if self.calls == 1 else "aa:bb"},
            }

    fault = _IntermittentFault()
    presence.bind_injected_problem("s-timeout", fault, None)
    try:
        assert presence.recheck_bound_artifact("s-timeout")["present"] is True
        assert fault.calls == 2
    finally:
        presence.clear_injected_problem("s-timeout")


def test_default_recheck_does_not_call_verify_lab() -> None:
    class _Lab(ProblemBase):
        root_cause_name = "link_down"

        def __init__(self) -> None:
            super().__init__()
            self.lab_calls = 0

        def verify_fault(self, params: object = None) -> dict:
            return {
                "verified": True,
                "fault_type": "link_down",
                "details": {"operstate": "down"},
            }

        def verify_lab(self) -> dict:
            self.lab_calls += 1
            return {"verified": True}

    problem = _Lab()
    result = problem.recheck_artifact()
    assert result["present"] is True
    assert result["scope"] == "artifact"
    assert problem.lab_calls == 0


def test_finalize_keeps_environment_invalid_with_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "run.json").write_text(
        json.dumps({"status": "running"}), encoding="utf-8"
    )
    (tmp_path / "submission.json").write_text(
        json.dumps({"root_causes": []}), encoding="utf-8"
    )
    (tmp_path / "ground_truth.json").write_text(
        json.dumps({"is_anomaly": True}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "nika.workflows.benchmark.run.close_session", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "nika.workflows.benchmark.run.Session",
        lambda: (_ for _ in ()).throw(RuntimeError("closed session unavailable")),
    )
    _finalize_post_inject_failure(
        session_id="s-final",
        session_dir=tmp_path,
        result_dir=None,
        error=EnvironmentInvalid(
            "before_cleanup: fault=link_down: fault artifact absent"
        ),
    )
    meta = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert meta["outcome"] == ENVIRONMENT_INVALID
    assert meta["status"] == "error"
    assert not (tmp_path / "eval_metrics.json").exists()


def _checks(root: Path) -> list[dict]:
    payload = json.loads((root / PRESENCE_FILENAME).read_text(encoding="utf-8"))
    return list(payload["checks"])
