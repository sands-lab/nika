"""Recovery ordering, retry, and registry contracts."""

from types import SimpleNamespace

import pytest

from nika.problems.multi import MultiFaultParams, MultiFaultProblem

pytestmark = pytest.mark.unit


def composite(faults):
    for fault in faults:
        fault.MULTI_INJECT_DWELL_SEC = 0
    problem = object.__new__(MultiFaultProblem)
    problem.sub_faults = faults
    problem.problem_names = [fault.root_cause_name for fault in faults]
    problem.root_cause_name = problem.problem_names
    problem._recovery_pending = []
    problem._resolved_params = MultiFaultParams()
    problem.check_runtime_compatible = lambda **kw: None
    problem.check_native_targets = lambda *args, **kw: None
    return problem


def test_partial_injection_recovers_attempted_faults_in_reverse_order():
    events = []

    def fault(name, fail=False):
        def inject():
            events.append(f"inject {name}")
            if fail:
                raise RuntimeError("partial injection")

        def recover():
            events.append(f"recover {name}")
            return {"verified": True}

        return SimpleNamespace(
            root_cause_name=name, inject_fault=inject, recover_fault=recover
        )

    problem = composite([fault("first"), fault("second", fail=True), fault("third")])
    with pytest.raises(RuntimeError, match="partial injection"):
        problem.inject_fault()
    assert problem.recover_fault()["verified"]
    assert problem.recover_fault()["verified"]
    assert events == [
        "inject first",
        "inject second",
        "recover second",
        "recover first",
    ]


def test_failed_recovery_preserves_snapshot_order_for_retry():
    events = []
    attempts = iter([False, True])
    first = SimpleNamespace(
        root_cause_name="first",
        inject_fault=lambda: None,
        recover_fault=lambda: events.append("first") or {"verified": True},
    )
    second = SimpleNamespace(
        root_cause_name="second",
        inject_fault=lambda: None,
        recover_fault=lambda: events.append("second") or {"verified": next(attempts)},
    )
    problem = composite([first, second])
    problem.inject_fault()
    assert problem.recover_fault()["verified"] is False
    assert events == ["second"]
    assert problem.recover_fault()["verified"] is True
    assert events == ["second", "second", "first"]


def test_registered_failures_define_recovery():
    from nika.problems.registry import list_avail_problem_instances
    from nika.problems.base import ProblemBase

    classes = set(list_avail_problem_instances().values())
    missing = sorted(
        cls.__name__
        for cls in classes
        if not callable(getattr(cls, "recover_fault", None))
        or cls.recover_fault is ProblemBase.recover_fault
    )
    assert missing == []


def test_pending_composite_injection_cannot_overwrite_recovery_snapshots():
    state = {"value": "healthy"}
    saved = {}

    def inject():
        saved["value"] = state["value"]
        state["value"] = "fault"

    def recover():
        state["value"] = saved["value"]
        return {"verified": True}

    child = SimpleNamespace(
        root_cause_name="fault", inject_fault=inject, recover_fault=recover
    )
    problem = composite([child])
    problem.inject_fault()
    with pytest.raises(RuntimeError, match="Recover pending injections"):
        problem.inject_fault()
    assert problem.recover_fault()["verified"]
    assert state["value"] == "healthy"
    problem.inject_fault()
    assert problem.recover_fault()["verified"]
    assert state["value"] == "healthy"
