"""Full-audit status rules and the release coverage report."""

from __future__ import annotations

import ast
from types import SimpleNamespace
from pathlib import Path

import pytest

from experiment.audit.coverage import cover_release, release_cases
from experiment.audit.environment import (
    CaseAudit,
    StageResult,
    admission_status,
    admits,
    classify_observation,
    identity_from_row,
)
from experiment.audit.live import (
    _artifact_stage,
    _baseline_path,
    _control_stage,
    _note_sibling_control,
)
from experiment.audit.matrix import audit_plan, diagnose
from experiment.audit.report_doc import DOC_PATH, render_environment_audit_doc

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
    audit.stages = [
        StageResult(stage="baseline_lab", status="pass"),
        StageResult(stage="baseline_path", status="pass"),
        StageResult(stage="inject_artifact", status="pass"),
        StageResult(stage="symptom", status="pass"),
        StageResult(stage="persistence_artifact", status="pass"),
        StageResult(stage="persistence_symptom", status="pass"),
        StageResult(stage="final_artifact", status="pass"),
        StageResult(stage="final_symptom", status="pass"),
        StageResult(
            stage="control_path", status="unsupported", reason="no_control_path"
        ),
    ]
    assert audit.admission() == "pass"
    audit.stages.pop(0)
    assert audit.admission() == "no_evidence"


def test_changed_symptom_contract_requires_a_new_live_audit() -> None:
    row = {
        "scenario": "campus_lan",
        "problem": "dns_record_error",
        "symptom_probe": "dns_answer",
    }
    old = CaseAudit(
        identity=identity_from_row(row),
        symptom_probe="artifact_only",
        stages=[StageResult(stage="symptom", status="no_evidence")],
    )
    assert cover_release([row], [old])[0]["admission"] == "not_run"
    row["symptom_probe"] = "artifact_only"
    assert cover_release([row], [old])[0]["admission"] == "not_run"


def test_release_report_lists_every_case() -> None:
    rows = release_cases("0.2.0")
    for text in (
        DOC_PATH.read_text(encoding="utf-8"),
        render_environment_audit_doc(records=[]),
    ):
        for row in rows:
            assert (
                f"| {row['problem']} |" in text
                or f"| {row['split']} | {row['problem']} |" in text
            )
        assert "Admitted cases:" in text
    assert admits("not_run") is False
    assert admits("no_evidence") is False
    assert admits("fail") is False


def test_audit_plan_covers_every_release_case() -> None:
    plan = audit_plan()
    release = release_cases("0.2.0")
    assert {identity_from_row(row).key() for row in plan} == {
        identity_from_row(row).key() for row in release
    }
    assert len(plan) == len(release)


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
    assert image.admission() == "unsupported"
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
            StageResult(stage="baseline_path", status="pass"),
            StageResult(stage="inject_artifact", status="pass"),
            StageResult(stage="symptom", status="pass"),
            StageResult(stage="persistence_artifact", status="pass"),
            StageResult(stage="persistence_symptom", status="pass"),
            StageResult(stage="final_artifact", status="pass"),
            StageResult(stage="final_symptom", status="pass"),
            StageResult(
                stage="control_path", status="unsupported", reason="no_control_path"
            ),
        ],
    )
    assert diagnose(behavioral) == "pass"


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
    assert (
        _control_stage("link_down", {"details": {"control_ok": False}}).status == "fail"
    )
    assert (
        _control_stage(
            "link_down", {"control_ok": True, "details": {"control_ok": False}}
        ).status
        == "fail"
    )
    assert _artifact_stage(
        "final_artifact", {"present": False, "error": "gone"}
    ).status == ("fail")


def test_failed_sibling_control_is_not_a_missing_control(monkeypatch) -> None:
    from experiment.audit import live

    monkeypatch.setattr(
        live,
        "_resolve_path",
        lambda *a, **k: SimpleNamespace(
            peer_host="peer", src_host="client", dst_ip="10.0.0.1", http_url=None
        ),
    )
    monkeypatch.setattr(live, "ping_ok", lambda *a, **k: False)
    payload = _note_sibling_control(
        None, "dc_clos", None, "s", {"verified": True}, None
    )
    assert _control_stage("link_down", payload).status == "fail"
    assert _control_stage("link_down", payload).evidence["path"] == {
        "source": "peer",
        "destination_ip": "10.0.0.1",
        "http_url": None,
    }
    # A different passing ping must not replace the probe's failed control.
    monkeypatch.setattr(live, "ping_ok", lambda *a, **k: True)
    payload = _note_sibling_control(
        None, "dc_clos", None, "s", {"details": {"control_ok": False}}, None
    )
    assert _control_stage("link_down", payload).status == "fail"


def test_control_avoids_destination_behind_root_cause_interface(monkeypatch) -> None:
    from nika.problems.rca.models import interface_resource
    from experiment.audit import live

    links = [
        ("a", ("leaf_1:eth4", "rcv:eth0")),
        ("b", ("leaf_1:eth5", "sib:eth0")),
        ("c", ("leaf_2:eth4", "other:eth0")),
    ]
    monkeypatch.setattr(live, "iter_link_termination_points", lambda net_env: links)
    ips = {"rcv": "10.0.1.11", "sib": "10.0.1.12", "other": "10.0.2.11"}
    runtime = SimpleNamespace(
        get_data_plane_host_ip=ips.get,
        get_host_ip=lambda n, i, **k: ips.get(n),
    )
    problem = SimpleNamespace(
        net_env=SimpleNamespace(hosts=["rcv", "sib", "other"], servers={}),
        root_cause_resources=lambda parsed: [interface_resource("leaf_1", "eth4")],
    )
    pick = live._independent_control_ip
    assert pick(problem, None, runtime, "peer", "10.0.1.11") == ("peer", "10.0.1.12")
    assert pick(problem, None, runtime, "peer", "10.0.2.11") == ("peer", "10.0.2.11")
    links[1:] = [("s", ("leaf_1:eth0", "spine:eth0"))]
    assert pick(problem, None, runtime, "peer", "10.0.1.11") is None


def test_control_from_far_end_avoids_the_faulted_device(monkeypatch) -> None:
    from nika.problems.rca.models import interface_resource
    from experiment.audit import live

    links = [
        ("core", ("n10:eth0", "n2:eth0")),
        ("e10", ("n10:eth6", "pc_n10:eth0")),
        ("e2", ("n2:eth3", "pc_n2:eth0")),
    ]
    monkeypatch.setattr(live, "iter_link_termination_points", lambda net_env: links)
    ips = {
        ("pc_n10", "eth0"): "10.254.0.6",
        ("pc_n2", "eth0"): "10.254.0.46",
        ("n2", "eth3"): "10.254.0.45",
    }
    runtime = SimpleNamespace(get_host_ip=lambda n, i, **k: ips.get((n, i)))
    problem = SimpleNamespace(
        net_env=SimpleNamespace(hosts=[], servers={}),
        root_cause_resources=lambda parsed: [interface_resource("n10", "eth0")],
    )
    pick = live._independent_control_ip
    assert pick(problem, None, runtime, "pc_n2", "10.254.0.46") == (
        "pc_n2",
        "10.254.0.45",
    )


def test_control_avoids_the_attacking_host(monkeypatch) -> None:
    from nika.problems.rca.models import node_resource
    from experiment.audit import live

    links = [
        ("a", ("client_0:eth0", "leaf_0:eth1")),
        ("b", ("client_1:eth0", "leaf_0:eth2")),
        ("c", ("web:eth0", "leaf_1:eth1")),
        ("d", ("dns:eth0", "leaf_1:eth2")),
    ]
    monkeypatch.setattr(live, "iter_link_termination_points", lambda net_env: links)
    ips = {"client_0": "10.0.0.2", "client_1": "10.0.0.3", "web": "10.0.1.2"}
    ips["dns"] = "10.0.1.3"
    runtime = SimpleNamespace(get_host_ip=lambda n, i, **k: ips.get(n))
    problem = SimpleNamespace(
        net_env=SimpleNamespace(hosts=[], servers={}),
        root_cause_resources=lambda parsed: [node_resource("web")],
    )
    params = {"attacker_device": "client_0", "host_name": "web"}
    source, dst = live._independent_control_ip(
        problem, params, runtime, "client_0", "10.0.1.2"
    )
    assert source not in {"client_0", "web"}
    assert dst not in {"10.0.0.2", "10.0.1.2"}


def test_control_avoids_a_faulted_link_and_uses_the_p2p_peer(monkeypatch) -> None:
    from nika.problems.rca.models import link_resource
    from experiment.audit import live

    links = [
        ("a", ("client1:eth1", "leaf1:e1-2")),
        ("b", ("client2:eth1", "leaf2:e1-2")),
        ("c", ("leaf1:e1-1", "spine1:e1-1")),
        ("d", ("leaf2:e1-1", "spine1:e1-2")),
    ]
    monkeypatch.setattr(live, "iter_link_termination_points", lambda net_env: links)
    ips = {"client1": "10.0.0.25", "client2": "10.0.0.27"}
    runtime = SimpleNamespace(
        get_host_ip=lambda n, i, with_prefix=False: (
            f"{ips[n]}/31" if with_prefix and n in ips else ips.get(n)
        ),
    )
    problem = SimpleNamespace(
        net_env=SimpleNamespace(hosts=["client1", "client2"], servers={}),
        root_cause_resources=lambda parsed: [link_resource("client1:eth1--leaf1:e1-2")],
    )
    pick = live._independent_control_ip
    assert pick(problem, None, runtime, "client2", "10.0.0.27") == (
        "client2",
        "10.0.0.26",
    )


def test_control_avoids_endpoints_behind_a_down_node(monkeypatch) -> None:
    from nika.problems.rca.models import FaultResource, ResourceKind
    from experiment.audit import live

    links = [
        ("a", ("leaf_1:eth2", "controller:eth0")),
        ("b", ("leaf_1:eth3", "worker1:eth0")),
        ("c", ("leaf_1:eth0", "spine:eth0")),
        ("d", ("leaf_2:eth0", "spine:eth1")),
        ("e", ("leaf_2:eth3", "worker3:eth0")),
        ("f", ("leaf_2:eth4", "worker4:eth0")),
        # A management segment the probe hosts are not on.
        ("oob", ("leaf_1:eth9", "leaf_2:eth9", "onos:eth0")),
    ]
    monkeypatch.setattr(live, "iter_link_termination_points", lambda net_env: links)
    ips = {
        "controller": "201.1.1.2",
        "worker1": "201.1.1.3",
        "worker3": "201.2.1.3",
        "worker4": "201.2.1.4",
        "onos": "172.31.0.100",
    }
    runtime = SimpleNamespace(get_host_ip=lambda n, i, **k: ips.get(n))
    problem = SimpleNamespace(
        net_env=SimpleNamespace(hosts=["client"], servers={}),
        root_cause_resources=lambda parsed: [
            FaultResource(kind=ResourceKind.NODE, node="leaf_1")
        ],
    )
    pick = live._independent_control_ip
    assert pick(problem, None, runtime, "as2r1", "201.1.1.2") == (
        "as2r1",
        "201.2.1.3",
    )
    # A source attached to the faulted node moves to an unaffected device.
    assert pick(problem, None, runtime, "worker1", "201.2.1.3") == (
        "worker4",
        "201.2.1.3",
    )


def test_control_avoids_faulted_host_and_self_ping(monkeypatch) -> None:
    from nika.problems.rca.models import interface_resource
    from experiment.audit import live

    lan = ("ctl:eth0", "client:eth0", "web:eth0")
    monkeypatch.setattr(live, "iter_link_termination_points", lambda e: [("l", lan)])
    ips = {"ctl": "200.0.0.2", "client": "200.0.0.7", "web": "200.0.0.8"}
    runtime = SimpleNamespace(
        get_data_plane_host_ip=lambda n: "10.210.0.7" if n == "ctl" else ips[n],
        get_host_ip=lambda n, i, **k: ips.get(n),
    )
    problem = SimpleNamespace(
        net_env=SimpleNamespace(hosts=["client"], servers={"web": ["web"]}),
        root_cause_resources=lambda parsed: [interface_resource("client", "eth0")],
    )
    pick = live._independent_control_ip
    assert pick(problem, None, runtime, "web", "200.0.0.7") == ("web", "200.0.0.2")
    assert pick(problem, None, runtime, "web", "200.0.0.8") == ("web", "200.0.0.2")


@pytest.mark.parametrize(
    "output", ["", "[TIMEOUT]", "ovs-vsctl: database connection failed"]
)
def test_southbound_read_errors_do_not_prove_disconnection(output) -> None:
    from tests.support.symptom.custom import _southbound_disconnected

    problem = SimpleNamespace(
        net_env=SimpleNamespace(model=SimpleNamespace(leaves=["leaf_1"], spines=[])),
        runtime=SimpleNamespace(exec=lambda *a, **k: output),
        root_cause_name="southbound_port_block",
    )
    ok, result = _southbound_disconnected(problem, None)
    assert ok is False
    assert result["error"] == "southbound_query_failed"


@pytest.mark.parametrize("probe", ["control_plane_bgp", "control_plane_ospf"])
def test_protocol_read_errors_are_not_observed_peer_losses(probe) -> None:
    from nika.net_env.base import ProbePath
    from tests.support.symptom.probe import run_probe_snapshot

    runtime = SimpleNamespace(exec=lambda *a, **k: "[TIMEOUT]")
    snapshot = run_probe_snapshot(
        runtime, probe, ProbePath(src_host="client", control_plane_host="router")
    )
    assert snapshot.control_plane_ok is None
    assert snapshot.extra["error"] in {"bgp_query_failed", "ospf_query_failed"}


def test_native_bgp_daemon_outage_is_an_observation() -> None:
    from nika.net_env.base import ProbePath
    from tests.support.symptom.probe import run_probe_snapshot

    runtime = SimpleNamespace(exec=lambda *a, **k: "bgpd is not running\n")
    snapshot = run_probe_snapshot(
        runtime,
        "control_plane_bgp",
        ProbePath(src_host="client", control_plane_host="router"),
    )
    assert snapshot.control_plane_ok is False
    assert snapshot.extra["bgp_query_ok"] is True
    assert snapshot.extra["bgp_established_peers"] == []


def test_audit_provenance_requires_a_finished_attributable_run() -> None:
    from experiment.audit.provenance import AuditProvenance, provenance_complete

    record = AuditProvenance(
        git_commit="a" * 40,
        git_dirty=False,
        source_sha256="source",
        configuration_sha256="config",
        started_at="2026-10-01T00:00:00+00:00",
        completed_at="2026-10-01T00:01:00+00:00",
        session_id="audit",
        images={"router": {"reference": "nika/frr:latest", "image_id": "sha256:old"}},
    )
    assert provenance_complete(record)
    assert not provenance_complete(None)
    assert not provenance_complete(record.model_copy(update={"completed_at": None}))
    assert not provenance_complete(record.model_copy(update={"git_commit": None}))
    assert not provenance_complete(record.model_copy(update={"images": {}}))


def test_production_audit_modules_do_not_import_tests() -> None:
    for relative in (
        "experiment/audit/environment.py",
        "experiment/audit/coverage.py",
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
