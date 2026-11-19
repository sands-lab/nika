"""Audit identities and evidence requirements for catalog combinations."""

from copy import deepcopy

import pytest

from nika.problems.combinations import get_combination
from nika.problems.multi import fault_instance_keys
from nika.workflows.benchmark.load_config import normalize_benchmark_row
from scripts.benchmark_audit.environment import (
    CaseAudit,
    StageResult,
    identity_from_row,
)
from scripts.benchmark_audit.matrix import _copy_row

pytestmark = pytest.mark.contract


def test_audit_preserves_nested_failures_and_timing():
    row = normalize_benchmark_row(get_combination("rov_gap_admits_hijack").rows()[0])
    copied = _copy_row(row)
    assert copied["schedule"] == row["schedule"]
    assert copied["problems"] == row["problems"]
    assert identity_from_row(copied).key() == identity_from_row(row).key()
    later = deepcopy(copied)
    later["schedule"]["events"][1]["at"] += 10
    assert identity_from_row(later).key() != identity_from_row(row).key()


@pytest.mark.parametrize("status", ["resolved", "unresolved"])
def test_audit_preserves_benchmark_case_image_identity(monkeypatch, status):
    from nika.firecracker import case
    from nika.workflows.benchmark.release import load_release

    row = deepcopy(
        next(
            r for r in load_release("0.3.0", split="test").cases if r.get("root_causes")
        )
    )
    row["root_causes_status"] = status
    if status == "unresolved":
        row.pop("root_causes")
    monkeypatch.setattr(case, "image_key", lambda params: "same-lab")
    copied = _copy_row(row)
    assert copied.get("root_causes") == row.get("root_causes")
    assert copied.get("root_causes_status") == status
    assert case.case_key(copied) == case.case_key(row)
    assert identity_from_row(copied).key() == identity_from_row(row).key()


def test_missing_licensed_image_is_distinct_from_public_image_preparation():
    from scripts.benchmark_audit.matrix import _failed_audit, diagnose

    row = {
        "scenario": "campus_lan",
        "problem": "ospf_hello_interval_mismatch+link_down",
    }
    message = (
        "XRd Control Plane image 'ios-xr/xrd-control-plane:26.2.1' not found locally."
    )
    audit = _failed_audit(row, message)
    assert audit.admission() == "unsupported"
    assert "licensed IOS XR image is missing" in diagnose(audit, message)
    assert "excludes licensed images" in diagnose(audit, message)


def test_combination_admission_requires_relation_oracle_and_recovery():
    row = normalize_benchmark_row(get_combination("rov_gap_admits_hijack").rows()[0])
    stages = [
        StageResult(stage=name, status="pass")
        for name in (
            "baseline_lab",
            "baseline_agent_view",
            "combination_matrix",
            "sham_oracle",
            *(f"neighbor_oracle:{key}" for key in fault_instance_keys(row["problems"])),
            "timeline",
            "inject_artifact",
            "leak_scan",
            "symptom",
            "oracle",
            "recovery",
            "final_lab",
        )
    ]
    audit = CaseAudit(
        identity=identity_from_row(row), stages=stages, symptom_probe="combination"
    )
    assert audit.admission() == "pass"
    for required in (
        "combination_matrix",
        "timeline",
        "oracle",
        "recovery",
        "sham_oracle",
        *(f"neighbor_oracle:{key}" for key in fault_instance_keys(row["problems"])),
    ):
        incomplete = audit.model_copy(
            update={"stages": [s for s in stages if s.stage != required]}
        )
        assert incomplete.admission() != "pass"


@pytest.mark.parametrize(
    "combination", ["rov_gap_admits_hijack", "redundant_links_both_lost"]
)
def test_case_image_requires_each_members_control(combination):
    row = normalize_benchmark_row(get_combination(combination).rows()[0])
    stages = [
        StageResult(stage=name, status="pass")
        for name in (
            "image_artifact",
            "image_symptom",
            *(("timeline",) if row.get("schedule") is not None else ()),
            "oracle",
            *(f"neighbor_oracle:{key}" for key in fault_instance_keys(row["problems"])),
            "image_persistence_artifact",
            "image_persistence_symptom",
            "image_final_artifact",
            "image_final_symptom",
        )
    ]
    audit = CaseAudit(identity=identity_from_row(row), mode="case_image", stages=stages)
    assert audit.admission() == "pass"
    audit.stages = [
        s
        for s in stages
        if s.stage != f"neighbor_oracle:{fault_instance_keys(row['problems'])[0]}"
    ]
    assert audit.admission() == "no_evidence"


@pytest.mark.parametrize(
    "timed,repeated,isp",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_case_image_keeps_single_fault_oracle_and_neighbor_checks(
    monkeypatch, timed, repeated, isp
):
    from types import SimpleNamespace
    from scripts.benchmark_audit import live

    class Lab:
        image = SimpleNamespace(key="lab")
        case = SimpleNamespace(key="case", session_id="baseline", injected=not timed)
        timings = {}

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def attach(self, *, session_id):
            return session_id

    synchronized = [not timed]

    class Timeline:
        def __init__(self, *args, **kwargs):
            self.complete = SimpleNamespace(is_set=lambda: synchronized[0])

        def prepare(self):
            pass

        def start(self):
            pass

        def wait_agent_start(self):
            pass

        def observation_revision(self):
            return 0

        def wait_for_progress(self, revision, timeout):
            synchronized[0] = True

        def raise_if_failed(self):
            pass

        def status(self):
            return {"completed": synchronized[0]}

        def close(self):
            pass

    monkeypatch.setattr("nika.remote.schedule.RemoteSessionScheduler", Timeline)

    session = SimpleNamespace(
        lab_name="restored-audit-copy",
        scenario_name="isp_sun" if isp else "campus_lan",
        scenario_params={"topo_size": "m", "topo": "sun"} if isp else {},
        topology_file="/deployed/topology.clab.yaml",
        runtime_workdir="/deployed/workdir",
        metadata={},
    )
    client = SimpleNamespace(
        fault_artifact=lambda sid: {"present": True, "evidence": {}},
        symptom=lambda *args, **kwargs: {"ok": True, "payload": {"verified": True}},
    )
    monkeypatch.setattr("nika.firecracker.trial.TrialLab", Lab)
    monkeypatch.setattr("nika.remote.workflows._client", lambda: client)
    monkeypatch.setattr(
        "nika.utils.session.Session",
        lambda: SimpleNamespace(load_running_session=lambda **kw: session),
    )
    monkeypatch.setattr(
        "nika.runtime.factory.resolve_backend", lambda session: "kathara"
    )

    def reconstruct(scenario, **kwargs):
        assert scenario == session.scenario_name
        assert kwargs == {"backend": "kathara", "lab_name": session.lab_name}
        return SimpleNamespace()

    monkeypatch.setattr("nika.net_env.net_env_pool.get_net_env_instance", reconstruct)
    monkeypatch.setattr("nika.workflows.session.close.close_session", lambda **kw: None)
    monkeypatch.setattr(live, "read_git_commit", lambda: ("a" * 40, False))
    monkeypatch.setattr(live.time, "sleep", lambda duration: None)
    seen = []

    def oracle(sid, expected_faults):
        assert expected_faults == {"link_down"}
        assert synchronized[0], "Oracle ran before the remote injection records arrived"
        seen.append("oracle")
        return StageResult(stage="oracle", status="pass")

    def neighbor(sid, fault, *args, active_faults):
        from pathlib import Path

        assert args[2].topology_file == Path(session.topology_file)
        assert args[2].runtime_workdir == Path(session.runtime_workdir)
        assert fault == "link_down" and active_faults == {fault}
        if repeated:
            assert args[0].inject == next(iter(remaining.values()))
            remaining.pop(next(iter(remaining)))
        seen.append("neighbor")
        return StageResult(stage="neighbor_oracle", status="pass")

    monkeypatch.setattr(live, "_oracle_stage", oracle)
    monkeypatch.setattr(live, "_neighbor_oracle_stage", neighbor)
    row = {
        "scenario": session.scenario_name,
        "topo_size": "m" if isp else "s",
        "problem": "link_down",
        "inject": {"host_name": "router_core_1", "intf_name": "eth0"},
    }
    if repeated:
        row = normalize_benchmark_row(
            get_combination("redundant_links_both_lost").rows()[0]
        )
    from nika.problems.combinations import task_placement
    from nika.schedule import schedule_for_task

    remaining = dict(task_placement(row)) if repeated else {}
    if timed:
        row["schedule"] = {
            "agent_start": 0,
            "events": [
                {"problem": event.problem, "at": 10, "inject": event.inject}
                for event in schedule_for_task(row).events
            ],
        }
    audit = live.audit_case_image(row)
    assert audit.admission() == "pass"
    assert seen == ["oracle", *(["neighbor"] * (2 if repeated else 1))]
    assert not remaining
