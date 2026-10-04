"""Lightweight fault-artifact checks for a running benchmark trial.

``verify_fault`` often reads state that ``inject_fault`` stored on the same
problem instance (ASN, proxy handle, applied sysctl, CPU quota). A new
instance does not have that state, so a recheck uses the instance that
injected the fault.

These checks read configuration, interface state, processes, rules, quotas,
or the fault's own worker. They do not run ``verify_lab``, symptom traffic,
or Batfish. A ``present`` result means the artifact is still there. It does
not mean the fault's network effect was measured.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nika.utils.logger import log_event
from nika.utils.session_artifacts import json_safe

DURING_AGENT_DELAY_SEC = 2.0

# Faults whose effect is a live worker, flap, or quota. The recheck reads
# that worker or setting on the injected instance. It does not replay traffic.
DYNAMIC_ARTIFACT_FAULTS = frozenset(
    {
        "link_flap",
        "link_capacity_bottleneck",
        "incast_traffic_network_limitation",
        "web_dos_attack",
        "tcp_syn_flood_attack",
        "load_balancer_overload",
        "sender_resource_contention",
        "receiver_resource_contention",
        "arp_cache_poisoning",
    }
)


_BOUND: dict[str, tuple[Any, Any]] = {}
_BOUND_LOCK = threading.Lock()


def bind_injected_problem(session_id: str, problem: Any, params: Any) -> None:
    """Keep the injecting instance for later artifact rechecks."""
    with _BOUND_LOCK:
        _BOUND[session_id] = (problem, params)


def clear_injected_problem(session_id: str) -> None:
    with _BOUND_LOCK:
        _BOUND.pop(session_id, None)


def bound_injected_problem(session_id: str) -> tuple[Any, Any] | None:
    with _BOUND_LOCK:
        return _BOUND.get(session_id)


def fault_label(problem: Any) -> str:
    name = getattr(problem, "root_cause_name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(name, (list, tuple)) and name:
        return ",".join(str(item) for item in name)
    return type(problem).__name__


def _anomaly_expected(session_dir: Path) -> bool:
    path = session_dir / "ground_truth.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(payload, dict) and payload.get("is_anomaly"))


def _remote_lab() -> bool:
    try:
        from nika.remote.config import is_remote_enabled

        return bool(is_remote_enabled())
    except Exception:  # noqa: BLE001 - remote config must not break a local trial
        return False


def log_presence_check(check: dict[str, Any]) -> None:
    """Log one artifact check as a ``fault_presence_recheck`` event."""
    safe = json_safe(check)
    log_event(
        "fault_presence_recheck",
        (
            f"Fault artifact {safe.get('result')}: "
            f"phase={safe.get('phase')} fault={safe.get('fault')}"
        ),
        phase=safe.get("phase"),
        fault=safe.get("fault"),
        result=safe.get("result"),
        scope="artifact",
        check=safe.get("check"),
        evidence=safe.get("evidence"),
        error=safe.get("error"),
    )


def record_injection_verify(*, fault: str, verify_result: dict[str, Any]) -> None:
    """Record the inject-time ``verify_fault`` result. Does not probe again."""
    details = verify_result.get("details") if isinstance(verify_result, dict) else {}
    log_presence_check(
        {
            "phase": "post_inject",
            "timestamp": datetime.now(UTC).isoformat(),
            "fault": fault,
            "result": "present",
            "scope": "artifact",
            "check": "verify_fault",
            "evidence": details if isinstance(details, dict) else {"details": details},
            "error": None,
        },
    )


def recheck_bound_artifact(session_id: str) -> dict[str, Any]:
    """Read artifacts on the bound instance. Never constructs a new problem."""
    bound = bound_injected_problem(session_id)
    if bound is None:
        return {
            "present": False,
            "fault": None,
            "scope": "artifact",
            "check": "recheck_artifact",
            "evidence": {},
            "error": "injected problem instance is not bound in this process",
            "unbound": True,
        }
    problem, params = bound
    fault = fault_label(problem)
    try:
        raw = recheck_artifact_with_retry(problem, params)
    except Exception as exc:  # noqa: BLE001 - the trial records the read error
        return {
            "present": False,
            "fault": fault,
            "scope": "artifact",
            "check": "recheck_artifact",
            "evidence": {},
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(raw, dict):
        return {
            "present": False,
            "fault": fault,
            "scope": "artifact",
            "check": "recheck_artifact",
            "evidence": {},
            "error": "recheck_artifact did not return a dict",
        }
    present = bool(raw.get("present"))
    error = raw.get("error")
    if not present and not error:
        error = "fault artifact absent"
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    if fault in DYNAMIC_ARTIFACT_FAULTS:
        evidence = {**evidence, "dynamic_artifact": True}
    return {
        "present": present,
        "fault": raw.get("fault") or fault,
        "scope": "artifact",
        "check": "recheck_artifact",
        "evidence": evidence,
        "error": error,
    }


def recheck_artifact_with_retry(problem: Any, params: Any) -> dict[str, Any]:
    """Read the artifact again once after an absent read; never replay injection.

    Many checks reduce a timed-out command to a boolean, so a busy container
    can read as absent once.
    """
    for attempt in range(2):
        raw = problem.recheck_artifact(params)
        if not isinstance(raw, dict) or raw.get("present"):
            return raw
        if attempt == 0:
            time.sleep(1.0)
    return raw


def _check_record(session_id: str, session_dir: Path, phase: str) -> dict[str, Any]:
    anomaly = _anomaly_expected(session_dir)
    bound = bound_injected_problem(session_id)
    timestamp = datetime.now(UTC).isoformat()
    if bound is None and not anomaly:
        return {
            "phase": phase,
            "timestamp": timestamp,
            "fault": "healthy",
            "result": "not_applicable",
            "scope": "artifact",
            "check": "recheck_artifact",
            "evidence": {"reason": "no fault injected"},
            "error": None,
        }
    if bound is None and anomaly and _remote_lab():
        try:
            from nika.remote.client import RemoteClient

            raw = RemoteClient().fault_artifact(session_id)
        except Exception as exc:  # noqa: BLE001 - record a failed bounded read
            raw = {
                "present": False,
                "fault": None,
                "evidence": {},
                "error": f"{type(exc).__name__}: {exc}",
            }
    else:
        raw = recheck_bound_artifact(session_id)
    error = str(raw.get("error") or "")
    if raw.get("present"):
        result = "present"
    elif raw.get("unbound") or (error and error != "fault artifact absent"):
        result = "error"
    else:
        result = "absent"
    return {
        "phase": phase,
        "timestamp": timestamp,
        "fault": raw.get("fault"),
        "result": result,
        "scope": "artifact",
        "check": "recheck_artifact",
        "evidence": raw.get("evidence") or {},
        "error": raw.get("error"),
    }


class PresenceWatch:
    """One during-agent read and one read before lab cleanup.

    The during-agent read waits ``delay_sec``, then runs once. If the agent
    returns first, ``finish`` performs that read before cleanup. Reads are
    only logged; they do not change the trial outcome.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str | Path,
        *,
        delay_sec: float | None = None,
    ) -> None:
        self.session_id = session_id
        self.session_dir = Path(session_dir)
        self.delay_sec = (
            DURING_AGENT_DELAY_SEC if delay_sec is None else float(delay_sec)
        )
        self._stop = threading.Event()
        self._claim_lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._done: set[str] = set()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if bound_injected_problem(self.session_id) is None:
            self._run_phase("post_inject")
        self._thread = threading.Thread(
            target=self._during,
            name=f"fault-presence-{self.session_id}",
            daemon=True,
        )
        self._thread.start()

    def _claim(self, phase: str) -> bool:
        with self._claim_lock:
            if phase in self._done:
                return False
            self._done.add(phase)
            return True

    def _run_phase(self, phase: str) -> None:
        if not self._claim(phase):
            return
        with self._read_lock:
            record = _check_record(self.session_id, self.session_dir, phase)
            log_presence_check(record)

    def _during(self) -> None:
        if self._stop.wait(self.delay_sec):
            return
        self._run_phase("during_agent")

    def cancel(self) -> None:
        """Stop the timer without reading artifacts."""
        self._stop.set()

    def finish(self) -> None:
        """Stop the timer, then read artifacts that have not been read yet."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=60.0)
        self._run_phase("during_agent")
        self._run_phase("before_cleanup")
