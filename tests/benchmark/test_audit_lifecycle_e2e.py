"""Real audit injection, file-time controls, and recovery across router platforms."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nika.cli.main import app
from nika.mcp.session_context import SESSION_ID_ENV
from nika.net_env.verify import verify_lab_with_retry
from nika.problems.context import init_problem
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.utils.session_store import SessionStore
from nika.validation.presence import bound_injected_problem
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import PreparedFailure
from nika.workflows.session.close import close_session
from scripts.benchmark_audit.agent_view import leak_scan_for
from scripts.benchmark_audit.live import _oracle_stage, audit_open_session
from tests.support.prerequisites import docker_available

pytestmark = [pytest.mark.integration, pytest.mark.nightly]


@pytest.mark.skipif(not docker_available(), reason="Docker required")
def test_oracle_rejects_a_recovered_fault(monkeypatch):
    from nika.workflows.failure.inject import inject_failure

    session_id = start_net_env(
        "simple_bgp", None, session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, session_id)
    try:
        inject_failure(
            ["link_down"],
            session_id=session_id,
            param_overrides={"host_name": "pc1", "intf_name": "eth0"},
        )
        present = _oracle_stage(session_id)
        assert present.status == "pass", present.model_dump()
        assert json.loads(Path(present.evidence["path"]).read_text())["solved"] is True
        result = CliRunner().invoke(
            app, ["exec", "--session_id", session_id, "pc1", "ip link set eth0 up"]
        )
        assert result.exit_code == 0, result.output
        stage = _oracle_stage(session_id)
        assert stage.status == "fail", stage.model_dump()
        oracle = json.loads(Path(stage.evidence["path"]).read_text())
        assert oracle["solved"] is False
        assert any(not fault["confirmed"] for fault in oracle["faults"])
    finally:
        close_session(session_id=session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker required")
def test_healthy_audit_requires_live_oracle(monkeypatch):
    session_id = start_net_env(
        "simple_bgp", None, session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, session_id)
    try:
        audit = audit_open_session(
            session_id, {"scenario": "simple_bgp", "problem": "healthy"}
        )
        assert audit.admission() == "pass", json.dumps(audit.model_dump(), indent=2)
        stage = next(stage for stage in audit.stages if stage.stage == "oracle")
        result = json.loads(Path(stage.evidence["path"]).read_text())
        assert result["solved"] is True
        assert result["faults"] and all(
            fault["confirmed"] for fault in result["faults"]
        )
    finally:
        close_session(session_id=session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker required")
@pytest.mark.parametrize(
    "scenario,backend,target,saved_config,oracle_solved",
    [
        ("simple_bgp", "kathara", "router1", "/etc/frr/frr.conf", False),
        ("min3clos", "containerlab", "leaf1", "/etc/opt/srlinux/config.json", True),
    ],
)
def test_audit_uses_recorded_injection_and_collects_time_leads(
    scenario, backend, target, saved_config, oracle_solved, monkeypatch
):
    if backend == "containerlab" and not shutil.which("clab"):
        pytest.skip("Containerlab required")
    session_id = start_net_env(
        scenario, None, backend=backend, session_tag=resolve_session_tag(context="test")
    )
    monkeypatch.setenv(SESSION_ID_ENV, session_id)
    try:
        session = Session().load_running_session(session_id=session_id)
        env, runtime = init_problem(session.scenario_name, **session.scenario_params)
        # A metadata-only mutation must be visible even when content is stable.
        scan = leak_scan_for(env, "bgp_asn_misconfig")
        scan.capture_before()
        touched = CliRunner().invoke(
            app, ["exec", "--session_id", session_id, target, f"touch {saved_config}"]
        )
        assert touched.exit_code == 0, touched.output
        leads = scan.capture_after_and_diff()
        assert (
            runtime.exec(target, f"md5sum {saved_config}").split()[0]
            == scan.before[target]["files"][saved_config]
        )
        assert any(
            item["node"] == target
            and item["path"] == saved_config
            and "mtime" in item["times"]
            for item in leads["file_time_asymmetries"]
        ), leads
        assert not any(item.get("path") == saved_config for item in leads["flagged"]), (
            leads
        )
        env.align_saved_configs()
        aligned = scan.capture_after_and_diff()
        assert not any(
            item["path"] == saved_config for item in aligned["file_time_asymmetries"]
        ), aligned
        row = {
            "scenario": scenario,
            "backend": backend,
            "problem": "bgp_asn_misconfig",
            "inject": {"host_name": target},
        }
        audit = audit_open_session(session_id, row)
        assert audit.admission() == ("pass" if oracle_solved else "fail"), json.dumps(
            audit.model_dump(), indent=2
        )
        stages = {stage.stage: stage for stage in audit.stages}
        for stage in audit.stages:
            if stage.stage == "oracle":
                continue
            assert stage.status == "pass" or (
                stage.stage == "control_path"
                and stage.status == "unsupported"
                and stage.reason == "no_control_path"
            ), stage.model_dump()
        assert stages["oracle"].status == ("pass" if oracle_solved else "fail")
        oracle = json.loads(Path(stages["oracle"].evidence["path"]).read_text())
        assert oracle["solved"] is oracle_solved
        assert all(fault["confirmed"] for fault in oracle["faults"]) is oracle_solved
        coverage = stages["leak_scan"].evidence["file_time_coverage"][target]
        assert coverage["before"] > 0 and coverage["after"] > 0, coverage
        assert not any(
            item["path"] == saved_config
            for item in stages["leak_scan"].evidence["file_time_asymmetries"]
        )
        rows = SessionStore().list_failure_injections(session_id=session_id)
        assert len(rows) == 1 and rows[0]["status"] == "injected", rows
        gt = json.loads((Path(session.session_dir) / "ground_truth.json").read_text())
        assert {cause["fault_type"] for cause in gt["root_causes"]} == {
            "bgp_asn_misconfig"
        }
        bound = bound_injected_problem(session_id)
        assert bound is not None
        problem, params = bound
        assert params.host_name == target
        assert PreparedFailure(problem, params).recover()["verified"]
        env.align_saved_configs()
        assert verify_lab_with_retry(env, full=True)["verified"]
    finally:
        close_session(session_id=session_id)
