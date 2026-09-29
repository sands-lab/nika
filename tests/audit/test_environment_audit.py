"""Full-audit status rules and the release coverage report."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nika.audit.coverage import release_cases
from nika.audit.environment import (
    CaseAudit,
    StageResult,
    admission_status,
    admits,
    classify_observation,
    identity_from_row,
)
from tests.audit.live import _artifact_stage, _baseline_path, _control_stage
from tests.audit.matrix import audit_plan, diagnose
from tests.audit.report_doc import DOC_PATH, render_environment_audit_doc

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_observation_statuses_stay_distinct() -> None:
    assert classify_observation(
        {"skipped": True, "reason": "artifact_only"}, ok=True
    ) == ("no_evidence", "artifact_only")
    assert classify_observation(
        {"skipped": True, "reason": "control_plane_only"}, ok=True
    ) == ("unsupported", "control_plane_only")
    assert classify_observation({"skipped": True, "reason": "manual"}) == (
        "skipped",
        "manual",
    )
    assert classify_observation(None, ok=True) == ("no_evidence", "missing observation")
    assert classify_observation({}, ok=True) == ("no_evidence", "missing observation")
    assert classify_observation({"verified": False}, ok=True)[0] == "fail"
    assert (
        classify_observation({"comparison": {"expect": "down"}}, ok=True)[0] == "pass"
    )


def test_admission_rejects_gaps() -> None:
    assert admits("pass") is True
    for status in ("fail", "skipped", "unsupported", "no_evidence", "not_run"):
        assert admits(status) is False
    assert admission_status(["pass", "no_evidence"]) == "no_evidence"
    assert admission_status(["pass", "skipped"]) == "skipped"
    assert admission_status(["pass", "unsupported"]) == "unsupported"
    assert admission_status(["unsupported", "fail"]) == "fail"
    assert admission_status([]) == "not_run"
    audit = CaseAudit(
        identity=identity_from_row(
            {"scenario": "dc_clos", "problem": "link_down", "topo_size": "s"}
        ),
        stages=[
            StageResult(stage="baseline_lab", status="pass"),
            StageResult(stage="symptom", status="no_evidence", reason="artifact_only"),
        ],
    )
    assert audit.admission() == "no_evidence"
    assert admits(audit.admission()) is False


def test_release_report_lists_every_case() -> None:
    text = render_environment_audit_doc()
    rows = release_cases("0.2.0")
    assert text == DOC_PATH.read_text(encoding="utf-8")
    for row in rows:
        assert (
            f"| {row['problem']} |" in text
            or f"| {row['split']} | {row['problem']} |" in text
        )
    assert "Admitted cases:" in text
    assert admits("not_run") is False
    assert admits("no_evidence") is False
    assert admits("fail") is False


def test_audit_plan_covers_every_scenario_and_failure() -> None:
    from nika.net_env.net_env_pool import list_all_net_envs
    from nika.problems.registry import list_avail_problem_names
    from nika.workflows.benchmark.admit import resource_class_for_row

    plan = audit_plan()
    healthy = [row for row in plan if row["problem"] == "healthy"]
    faults = [row for row in plan if row["problem"] != "healthy"]
    assert {row["scenario"] for row in healthy} == set(list_all_net_envs())
    assert {row["problem"] for row in faults} == set(list_avail_problem_names())
    link_down = next(row for row in faults if row["problem"] == "link_down")
    assert resource_class_for_row(link_down) == "light"
    arp = next(row for row in faults if row["problem"] == "arp_cache_poisoning")
    assert resource_class_for_row(arp) == "light"


def test_diagnose_separates_check_from_fault() -> None:
    identity = identity_from_row(
        {"scenario": "campus_lan", "problem": "frr_service_down", "topo_size": "s"}
    )
    down = CaseAudit(
        identity=identity,
        symptom_probe="control_plane_bgp",
        stages=[
            StageResult(stage="baseline_lab", status="pass"),
            StageResult(
                stage="baseline_path",
                status="fail",
                evidence={"control_plane_ok": False, "control_ok": None},
            ),
        ],
    )
    assert diagnose(down) == "verify: control_plane_bgp was already down before inject"
    image = CaseAudit(
        identity=identity_from_row(
            {"scenario": "routeros_simple_bgp", "problem": "healthy"}
        ),
        stages=[StageResult(stage="audit", status="unsupported", reason="missing")],
    )
    assert (
        diagnose(image, "RuntimeError: image not found locally")
        == "verify: the scenario image is not installed on this host"
    )
    behavioral = CaseAudit(
        identity=identity_from_row(
            {"scenario": "campus_lan", "problem": "arp_cache_poisoning"}
        ),
        stages=[
            StageResult(stage="baseline_lab", status="pass"),
            StageResult(stage="symptom", status="pass"),
            StageResult(
                stage="control_path", status="unsupported", reason="no_control_path"
            ),
        ],
    )
    assert "no separate control path" in diagnose(behavioral)


def test_control_and_baseline_helpers() -> None:
    class _Snap:
        ping_ok = True
        http_ok = None
        control_plane_ok = None

        def as_dict(self) -> dict:
            return {"ping_ok": True}

    assert _baseline_path("path_ping", _Snap()).status == "pass"
    assert _baseline_path("artifact_only", _Snap()).status == "no_evidence"
    assert _control_stage("link_down", {"comparison": {}}).status == "unsupported"
    assert _control_stage("link_down", {"after": {"control_ok": None}}).status == (
        "unsupported"
    )
    assert _control_stage("link_down", {"control_ok": False}).status == "fail"
    assert _artifact_stage(
        "final_artifact", {"present": False, "error": "gone"}
    ).status == ("fail")


def test_production_audit_modules_do_not_import_tests() -> None:
    for relative in (
        "src/nika/audit/environment.py",
        "src/nika/audit/coverage.py",
        "src/nika/validation/presence.py",
        "src/nika/workflows/benchmark/run.py",
        "src/nika/workflows/agent/run.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert all(not name.startswith("tests") for name in names), relative
