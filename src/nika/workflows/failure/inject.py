"""Inject configured faults for the current session and write ground truth."""

import json
import time
from datetime import datetime
from enum import Enum
from typing import Any

from nika.problems.prob_pool import (
    get_problem_instance,
    list_avail_problem_names,
    resolve_problem_name,
)
from nika.utils.logger import bind_session_dir, log_error_event, log_event
from nika.utils.session import Session
from nika.utils.session_store import SessionStore


def _failure_verify_settings() -> tuple[int, float]:
    try:
        from nika.run_config.loader import get_run_config

        lab = get_run_config().nika.lab
        return int(lab.failure_verify_max_attempts), float(
            lab.failure_verify_retry_delay_sec
        )
    except Exception:  # noqa: BLE001
        return 3, 5.0


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def _extract_injection_params(problem: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"problem_class": problem.__class__.__name__}
    for attr in (
        "faulty_devices",
        "faulty_intf",
        "intf_name",
        "service_name",
        "attacker_device",
        "target_host",
        "target_website",
        "target_domain",
        "p4_name",
        "southbound_port",
        "original_port",
        "mismatched_port",
        "deleted_subnet",
        "k8s_control_node",
        "k8s_objects",
        "k8s_namespace",
        "k8s_workload",
    ):
        if hasattr(problem, attr):
            params[attr] = _json_safe(getattr(problem, attr))
    return params


def _verify_with_retry(inject_problem: Any, fault_params: Any | None) -> dict[str, Any]:
    """Retry ``verify_fault`` while container state settles."""
    max_attempts, retry_delay = _failure_verify_settings()
    last_result: dict[str, Any] = {"verified": False}
    for attempt in range(1, max_attempts + 1):
        if fault_params is not None:
            last_result = inject_problem.verify_fault(params=fault_params)
        else:
            last_result = inject_problem.verify_fault()
        if last_result.get("verified", False):
            return last_result
        if attempt < max_attempts:
            time.sleep(retry_delay)
    return last_result


def inject_failure(
    problem_names: list[str],
    *,
    session_id: str | None = None,
    param_overrides: dict[str, str] | None = None,
    expected_root_causes: list | None = None,
) -> None:
    """Inject faults for ``problem_names`` into the lab for the running session."""
    from nika.remote.config import is_remote_enabled

    if is_remote_enabled():
        from nika.remote.workflows import remote_inject_failure

        remote_inject_failure(
            problem_names,
            session_id=session_id,
            param_overrides=param_overrides,
        )
        return

    session = Session()
    session.load_running_session(session_id=session_id)

    resolved_names = [resolve_problem_name(name) for name in problem_names]
    for original, resolved in zip(problem_names, resolved_names, strict=True):
        if resolved not in list_avail_problem_names():
            raise ValueError(f"Unknown problem name: {original}")

    session.update_session("problem_names", resolved_names)

    bind_session_dir(session.session_dir)

    store = SessionStore()

    scenario_params = dict(
        session.scenario_params if hasattr(session, "scenario_params") else {}
    )
    from nika.runtime.factory import resolve_backend

    session_meta = {k: v for k, v in session.__dict__.items() if k != "store"}
    scenario_params.setdefault("backend", resolve_backend(session_meta))
    overrides = dict(param_overrides or {})
    if overrides and len(resolved_names) != 1:
        raise ValueError(
            "When using --set parameters, inject exactly one problem at a time."
        )

    inject_problem = get_problem_instance(
        problem_names=resolved_names,
        scenario_name=session.scenario_name,
        **scenario_params,
    )
    if getattr(inject_problem, "root_cause_category", None):
        session.update_session(
            "root_cause_category", str(inject_problem.root_cause_category)
        )

    failure_rows: list[tuple[int, str]] = []
    now_ts = datetime.now().timestamp()
    ParamsClass = getattr(type(inject_problem), "Params", None)
    if hasattr(inject_problem, "resolve_params"):
        fault_params = inject_problem.resolve_params(overrides)
    elif ParamsClass is not None:
        fault_params = ParamsClass(**overrides)
    elif overrides:
        raise ValueError(
            f"Problem '{resolved_names[0]}' does not accept --set parameters yet."
        )
    else:
        fault_params = None

    if len(resolved_names) > 1 and hasattr(inject_problem, "sub_faults"):
        sub_faults = list(getattr(inject_problem, "sub_faults"))
        for idx, problem_name in enumerate(resolved_names):
            sub_problem = sub_faults[idx] if idx < len(sub_faults) else inject_problem
            failure_id = store.create_failure_injection(
                {
                    "session_id": session.session_id,
                    "problem_name": problem_name,
                    "root_cause_category": str(
                        getattr(sub_problem, "root_cause_category", "")
                    ),
                    "scenario_name": session.scenario_name,
                    "lab_name": session.lab_name,
                    "injection_params": _extract_injection_params(sub_problem),
                    "status": "pending",
                    "start_time": now_ts,
                }
            )
            failure_rows.append((failure_id, problem_name))
    else:
        params_snapshot = _extract_injection_params(inject_problem)
        if fault_params is not None:
            params_snapshot["resolved_params"] = _json_safe(
                fault_params.model_dump(exclude_none=True)
            )
        if overrides:
            params_snapshot["requested_overrides"] = _json_safe(overrides)
        if ParamsClass is not None:
            params_snapshot["param_schema"] = ParamsClass.__name__
        failure_id = store.create_failure_injection(
            {
                "session_id": session.session_id,
                "problem_name": resolved_names[0],
                "root_cause_category": str(
                    getattr(inject_problem, "root_cause_category", "")
                ),
                "scenario_name": session.scenario_name,
                "lab_name": session.lab_name,
                "injection_params": params_snapshot,
                "status": "pending",
                "start_time": now_ts,
            }
        )
        failure_rows.append((failure_id, resolved_names[0]))

    if ParamsClass is not None:
        try:
            inject_problem.inject_fault(params=fault_params)
        except Exception as exc:
            for failure_id, problem_name in failure_rows:
                store.update_failure_injection(
                    session.session_id,
                    failure_id,
                    {"status": "inject_failed", "error": str(exc)},
                )
            log_error_event(
                "failure_inject_error",
                f"Failure injection failed: session={session.session_id}, problems={problem_names}: {exc}",
                session_id=session.session_id,
                problems=problem_names,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise
    else:
        try:
            inject_problem.inject_fault()
        except Exception as exc:
            for failure_id, problem_name in failure_rows:
                store.update_failure_injection(
                    session.session_id,
                    failure_id,
                    {"status": "inject_failed", "error": str(exc)},
                )
            log_error_event(
                "failure_inject_error",
                f"Failure injection failed: session={session.session_id}, problems={problem_names}: {exc}",
                session_id=session.session_id,
                problems=problem_names,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

    if not hasattr(inject_problem, "verify_fault"):
        raise RuntimeError(f"Problem {problem_names} does not implement verify_fault")

    verify_result = _verify_with_retry(inject_problem, fault_params)
    verify_payload = _json_safe(verify_result)
    if not verify_result.get("verified", False):
        for failure_id, _problem_name in failure_rows:
            store.update_failure_injection(
                session.session_id,
                failure_id,
                {"status": "verify_failed", "verify_result": verify_payload},
            )
        log_error_event(
            "failure_verify_failed",
            f"Failure verification failed: session={session.session_id}, problems={problem_names}",
            session_id=session.session_id,
            problems=problem_names,
            verify_result=verify_payload,
        )
        hint = ""
        if "[TIMEOUT]" in json.dumps(verify_payload):
            # Distinguish a verifier timeout from evidence that injection failed.
            hint = (
                " NOTE: the verification command itself timed out inside the "
                "container ('[TIMEOUT]' in details) — this is usually a "
                "transient readiness/load issue, not a bad case: retry the "
                "case, or raise nika.lab.failure_verify_max_attempts / "
                "nika.lab.failure_verify_retry_delay_sec in config/nika.yaml."
            )
        raise RuntimeError(
            f"Failure injection verification failed for {problem_names}: "
            f"{verify_result}.{hint}"
        )

    for failure_id, problem_name in failure_rows:
        store.update_failure_injection(
            session.session_id,
            failure_id,
            {"status": "injected", "verify_result": verify_payload},
        )
        log_event(
            "failure_injected",
            f"Failure injected: session={session.session_id}, problem={problem_name}",
            session_id=session.session_id,
            problem=problem_name,
        )
    log_event(
        "failure_verified",
        f"Failure verified: session={session.session_id}, problems={problem_names}",
        session_id=session.session_id,
        problems=problem_names,
        verify_result=verify_payload,
    )

    log_event(
        "failure_inject_complete",
        f"Session {session.session_id}: injected {problem_names} under {session.scenario_name}.",
        session_id=session.session_id,
        problems=problem_names,
        scenario=session.scenario_name,
    )
    task_description = inject_problem.get_task_description()
    session.update_session("task_description", task_description)

    gt = inject_problem.get_ground_truth()
    if expected_root_causes:
        from nika.problems.ground_truth import assert_root_causes_match

        assert_root_causes_match(gt, expected_root_causes)
    session.write_gt(gt.model_dump(mode="json", exclude_none=True))
    log_event(
        "ground_truth_saved",
        f"Ground truth saved for session {session.session_id}.",
        session_id=session.session_id,
    )
