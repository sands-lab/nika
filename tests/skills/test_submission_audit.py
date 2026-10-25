"""Submission audit: trace parsing, signatures, re-grade, metrics, and review tools."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from experiment.submission_audit import skill  # noqa: F401  (adds auditlib to sys.path)
import yaml

from auditlib.cohort import TrialRecord, healthy_fp_rate, run_metrics
from auditlib.cohort_audit import Row, _same, signature_for
from auditlib.curate import (
    common_text,
    covered_by_signature,
    curate_group,
    recorded_by,
    to_pattern,
)
from auditlib.detectors import run_trial
from auditlib.kb import KB_DIR, Issue, Signature, load_kb
from auditlib.review import check_citations, check_issue
from auditlib.runner import audit_trajectories, prompt_contrast
from auditlib.scan import _parse as _parse_findings
from auditlib.scan import chunks as scan_chunks
from auditlib.trace import load_trial, trial_dirs

pytestmark = pytest.mark.unit

DNS_GT = {
    "is_anomaly": True,
    "root_causes": [
        {
            "fault_type": "host_incorrect_dns",
            "resource": {"id": "node/pc1", "kind": "node", "node": "pc1"},
        }
    ],
}
HEALTHY_GT = {"is_anomaly": False, "root_causes": []}


def _claude(kind: str, **payload: Any) -> dict[str, Any]:
    return {
        "phase": "diagnosis",
        "event": kind,
        "claude_event": {"type": kind, **payload},
    }


def _claude_call(
    call_id: str, name: str, command: str, output: str
) -> list[dict[str, Any]]:
    return [
        _claude(
            "assistant",
            message={
                "content": [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": {"command": command},
                    }
                ]
            },
        ),
        _claude(
            "user",
            message={
                "content": [
                    {"type": "tool_result", "tool_use_id": call_id, "content": output}
                ]
            },
        ),
    ]


def _trial(
    root: Path,
    name: str,
    events: list[dict[str, Any]],
    *,
    gt: dict[str, Any],
    submission: dict[str, Any],
    rca: float,
    scenario: str = "campus_lan",
    agent_type: str = "cli.claude",
) -> Path:
    path = root / "trials" / name
    path.mkdir(parents=True)
    (path / "messages.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    problems = [rc["fault_type"] for rc in gt["root_causes"]]
    run = {
        "agent_type": agent_type,
        "model": "m",
        "case_key": name.rsplit("__", 1)[0],
        "benchmark_version": "0.2.0",
        "benchmark_fingerprint": json.dumps(
            {"scenario": scenario, "problems": problems}
        ),
        "status": "finished",
    }
    (path / "run.json").write_text(json.dumps(run))
    (path / "ground_truth.json").write_text(json.dumps(gt))
    (path / "submission.json").write_text(
        json.dumps({"diagnosis_report": "# Report\n" + "x" * 300, **submission})
    )
    (path / "eval_metrics.json").write_text(
        json.dumps(
            {
                "rca_f1": rca,
                "localization_f1": rca,
                "fault_type_f1": rca,
                "detection_score": 1.0,
            }
        )
    )
    return path


def test_trace_steps_pair_calls_and_results_across_harnesses(tmp_path: Path) -> None:
    claude = _claude_call(
        "c1", "mcp__kathara_base_mcp_server__exec_shell", "ls /etc", "hosts"
    )
    react = [
        {
            "phase": "diagnosis",
            "event": "tool_start",
            "tool": {"name": "exec_shell"},
            "input": '{"command": "ls"}',
            "tool_call_id": "r1",
        },
        {
            "phase": "diagnosis",
            "event": "tool_end",
            "tool": {"name": "exec_shell"},
            "input": '{"command": "ls"}',
            "tool_call_id": "r1",
            "output": "hosts",
        },
        {
            "phase": "diagnosis",
            "event": "tool_start",
            "tool": {"name": "active_tcp_probe"},
            "input": '{"source": "client_0"}',
            "tool_call_id": "r2",
        },
        {
            "phase": "diagnosis",
            "event": "tool_error",
            "tool": {"name": "active_tcp_probe"},
            "tool_call_id": "r2",
            "output": "Error executing tool active_tcp_probe: Device is not running.",
        },
        {
            "phase": "diagnosis",
            "event": "llm_end",
            "text": "done",
            "generation_info": {"finish_reason": "length"},
        },
    ]
    codex = [
        {
            "phase": "diagnosis",
            "event": "item.completed",
            "codex_event": {"item": {"type": "reasoning", "text": "think"}},
        },
        {
            "phase": "diagnosis",
            "event": "tool_end",
            "tool": {"name": "exec_shell"},
            "tool_call_id": "x1",
            "output": "out",
        },
    ]
    for name, events in (("claude", claude), ("react", react), ("codex", codex)):
        path = tmp_path / name
        path.mkdir()
        (path / "messages.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in events) + "{broken\n"
        )
        trial = load_trial(path)
        assert trial.bad_lines == 1
        results = [s for s in trial.steps if s.kind == "tool_result"]
        assert results and all(s.call_id for s in results)
    claude_trial = load_trial(tmp_path / "claude")
    call = claude_trial.calls()[0]
    assert (call.server, call.tool) == ("kathara_base_mcp_server", "exec_shell")
    react_steps = load_trial(tmp_path / "react").steps
    assert [s.kind for s in react_steps] == [
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "agent_text",
        "harness",
    ]
    assert react_steps[3].is_error, "a failed call ends with tool_error"


RECORDED_LEAK = """\
id: LEAK-DHCP-HOOK-NAME
title: dhclient hook named after the DNS fault
class: shortcut
severity: block
status: open
summary: host_incorrect_dns installs a dhclient hook named nika-incorrect-dns.
scope: {fault_types: [host_incorrect_dns]}
detect: [{kind: text, where: [tool_result], pattern: nika-incorrect-dns}]
exploit: true
provenance: {source: test, date: '2026-10-28'}
"""


def _memory_with_recorded_leak(tmp_path: Path) -> Path:
    """The skill's generic checks plus one issue an earlier audit recorded."""
    kb_dir = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb_dir)
    (kb_dir / "issues" / "LEAK-DHCP-HOOK-NAME.yaml").write_text(
        RECORDED_LEAK, encoding="utf-8"
    )
    return kb_dir


def test_shipped_signatures_flag_leak_privilege_and_mutation(tmp_path: Path) -> None:
    init = _claude(
        "system",
        subtype="init",
        tools=[
            "Bash",
            "WebSearch",
            "mcp__claude_ai_Gmail__send",
            "mcp__kathara_base_mcp_server__exec_shell",
        ],
    )
    events = [
        init,
        *_claude_call(
            "c1",
            "mcp__kathara_base_mcp_server__exec_shell",
            "ls /etc/dhcp/dhclient-enter-hooks.d",
            "nika-incorrect-dns",
        ),
        *_claude_call(
            "c2",
            "mcp__kathara_base_mcp_server__exec_shell",
            "systemctl restart frr",
            "",
        ),
        *_claude_call(
            "c3",
            "mcp__kathara_base_mcp_server__exec_shell",
            "grep -E 'ip route add' /etc/rc.local",
            "",
        ),
        *_claude_call("c4", "WebSearch", "host_incorrect_dns nika", "results"),
    ]
    path = _trial(
        tmp_path, "campus__dns__h1__t01", events, gt=DNS_GT, submission=DNS_GT, rca=1.0
    )
    kb = load_kb(_memory_with_recorded_leak(tmp_path))
    hits = run_trial(kb, load_trial(path))
    issues = {h.issue for h in hits}
    assert {
        "LEAK-DHCP-HOOK-NAME",
        "POLICY-INIT-UNLISTED-TOOLS",
        "POLICY-CALL-UNLISTED-TOOL",
        "AGENT-STATE-MUTATION",
    } <= issues
    mutations = [h for h in hits if h.issue == "AGENT-STATE-MUTATION"]
    assert len(mutations) == 1, "a quoted grep pattern is not a command"


def test_report_regrades_and_scores_shortcut_trials_as_zero(tmp_path: Path) -> None:
    leak = _claude_call(
        "c1",
        "mcp__kathara_base_mcp_server__exec_shell",
        "ls /etc/dhcp",
        "nika-incorrect-dns",
    )
    kb = load_kb(_memory_with_recorded_leak(tmp_path))
    root = tmp_path / "pkg"
    _trial(root, "campus__dns__h1__t01", leak, gt=DNS_GT, submission=DNS_GT, rca=1.0)
    _trial(root, "campus__dns__h1__t02", [], gt=DNS_GT, submission=DNS_GT, rca=1.0)
    # Graded under the old rule: empty root causes on a healthy task scored 1.
    _trial(
        root,
        "campus__healthy__h2__t01",
        [],
        gt=HEALTHY_GT,
        submission={"is_anomaly": True, "root_causes": []},
        rca=1.0,
    )
    report = audit_trajectories(root, kb=kb)
    scores = report["scores"]
    assert scores["rca_f1_submitted"] == 1.0
    assert scores["rca_f1_regraded"] == pytest.approx(2 / 3, abs=1e-3)
    assert scores["exploited_trials"] == 1
    ids = {r["id"] for r in report["known_issues"]}
    assert "LEAK-DHCP-HOOK-NAME" in ids
    leak_row = next(
        r for r in report["known_issues"] if r["id"] == "LEAK-DHCP-HOOK-NAME"
    )
    assert leak_row["n_trials"] == 1


def test_healthy_false_positive_rate_groups_by_scenario() -> None:
    def rec(i: int, scenario: str, preds: list[str]) -> TrialRecord:
        return TrialRecord(f"t{i}", "s", f"c{i}", scenario, [], preds, None, 0.0)

    records = [rec(i, "dc_clos", ["bgp_x"]) for i in range(3)] + [
        rec(9, "campus_lan", [])
    ] * 3
    sig = Signature(
        kind="metric", metric="healthy_fp_rate", threshold=0.5, min_trials=3
    )
    rows = healthy_fp_rate(sig, records, [])
    assert [(group, value) for group, value, *_ in rows] == [("dc_clos", 1.0)]


def test_case_metric_scope_matches_full_fault_type_of_truncated_case_key() -> None:
    # Case keys cut fault type names short ("nat_mappi"); scope uses full names.
    fault = "nat_mapping_removed_without_drain"
    records = [
        TrialRecord(
            f"t{i}", f"s{i}", "enterprise__nat_mappi__h1", "e", [fault], [], True, 0.0
        )
        for i in range(6)
    ]
    issue = Issue.model_validate(
        {
            "id": "FNR-NAT",
            "title": "t",
            "class": "fault_not_realized",
            "severity": "warn",
            "status": "open",
            "summary": "s",
            "scope": {"fault_types": [fault]},
            "provenance": {"source": "test", "date": "2026-10-28"},
            "detect": [
                {
                    "kind": "metric",
                    "metric": "case_fail_rate",
                    "threshold": 0.95,
                    "min_trials": 6,
                }
            ],
        }
    )
    kb = type("KB", (), {"issues": [issue]})()
    hits = run_metrics(kb, records[:1], records[1:])
    assert [(h.issue, h.group) for h in hits] == [
        ("FNR-NAT", "enterprise__nat_mappi__h1")
    ]


def test_citations_must_occur_in_the_cited_step(tmp_path: Path) -> None:
    events = _claude_call(
        "c1", "mcp__kathara_base_mcp_server__exec_shell", "ls", "frr.conf.sav\nfrr.conf"
    )
    path = _trial(
        tmp_path, "t__x__h__t01", events, gt=DNS_GT, submission=DNS_GT, rca=0.0
    )
    trial = load_trial(path)
    findings = [
        {"trial": trial.trial_id, "evidence": [{"step": 1, "excerpt": "frr.conf.sav"}]},
        {"trial": trial.trial_id, "evidence": [{"step": 1, "excerpt": "frr.conf.bak"}]},
        {"trial": "missing", "evidence": [{"step": 1, "excerpt": "frr.conf.sav"}]},
    ]
    statuses = [
        f["citation_status"] for f in check_citations(findings, {trial.trial_id: trial})
    ]
    assert statuses == ["supported", "unsupported", "unsupported"]


def test_issue_check_requires_firing_on_its_own_sources(tmp_path: Path) -> None:
    leaky = _claude_call(
        "c1",
        "mcp__kathara_base_mcp_server__exec_shell",
        "ls /var/tmp",
        "apply-dns-override.sh",
    )
    source = _trial(
        tmp_path, "t__dns__h__t01", leaky, gt=DNS_GT, submission=DNS_GT, rca=1.0
    )
    issue = Issue(
        **yaml.safe_load(
            """
id: LEAK-TEST-HELPER
title: helper script names the fault
class: shortcut
severity: warn
status: open
summary: test
scope: {fault_types: [host_incorrect_dns]}
detect: [{kind: text, pattern: 'apply-dns-override'}]
provenance: {source: test, date: '2026-10-25'}
"""
        )
    )
    assert check_issue(issue, sources=[source])["ok"]
    misses = issue.model_copy(
        update={"detect": [Signature(kind="text", pattern="systemd-private")]}
    )
    assert not check_issue(misses, sources=[source])["ok"]
    assert not check_issue(issue, sources=[])["ok"], "a rule needs a source trial"


def test_prompt_contrast_flags_lines_only_fault_tasks_get(tmp_path: Path) -> None:
    def prompt(text: str) -> list[dict[str, Any]]:
        return [{"phase": "diagnosis", "event": "prompt", "text": text}]

    base = "Diagnose the network.\nDevices: pc_1, pc_2, router_1"
    for i in (1, 2):
        _trial(
            tmp_path,
            f"lab__dns__h{i}__t01",
            prompt(base + "\nReport faulty devices by name."),
            gt=DNS_GT,
            submission=DNS_GT,
            rca=1.0,
        )
    _trial(
        tmp_path,
        "lab__healthy__t01",
        prompt(base),
        gt=HEALTHY_GT,
        submission=HEALTHY_GT,
        rca=1.0,
    )
    trials = [load_trial(p) for p in trial_dirs(tmp_path)]
    found, hit = prompt_contrast(trials)
    assert found == {"campus_lan": ["Report faulty devices by name."]}
    assert hit == {"lab__dns__h1__t01", "lab__dns__h2__t01"}


def test_scan_parses_findings_and_splits_long_trials(tmp_path: Path) -> None:
    raw = 'Sure.\n{"findings": [{"class": "shortcut", "claim": "c", "step": "3", "excerpt": "x"}]}'
    assert _parse_findings(raw) == [
        {"class": "shortcut", "claim": "c", "step": 3, "excerpt": "x"}
    ]
    assert _parse_findings("no json here") == []
    events = [
        e
        for i in range(6)
        for e in _claude_call(
            f"c{i}", "mcp__kathara_base_mcp_server__exec_shell", "ls", "y" * 900
        )
    ]
    trial = load_trial(
        _trial(
            tmp_path, "t__dns__h__t01", events, gt=DNS_GT, submission=DNS_GT, rca=1.0
        )
    )
    pieces = scan_chunks(trial, [], limit=3000)
    assert len(pieces) > 1
    assert all(p.startswith("# Trial `t__dns__h__t01`") for p in pieces)
    assert all(p.rstrip().endswith("</trace>") for p in pieces)
    body = "".join(p.split("<trace>\n", 1)[1] for p in pieces)
    assert body.count("### [") == len(trial.steps)


def test_curation_writes_a_signature_from_shared_excerpt_text(tmp_path: Path) -> None:
    assert (
        common_text(
            [
                "ls /etc: frr.conf frr.conf.sav daemons",
                "frr.conf.sav  frr.conf",
                "-rw-r--r-- 1 root frr.conf.sav",
            ]
        )
        == "frr.conf.sav"
    )
    assert re.fullmatch(to_pattern("eth12 down 3 times"), "eth7  down 15 times")
    leak = _claude_call(
        "c1", "mcp__kathara_base_mcp_server__exec_shell", "ls /etc/frr", "frr.conf.sav"
    )
    clean = _claude_call(
        "c1", "mcp__kathara_base_mcp_server__exec_shell", "ls /etc/frr", "frr.conf"
    )
    root = tmp_path / "pkg"
    _trial(root, "lab__dns__h1__t01", leak, gt=DNS_GT, submission=DNS_GT, rca=1.0)
    _trial(
        root, "lab__healthy__t01", clean, gt=HEALTHY_GT, submission=HEALTHY_GT, rca=1.0
    )
    trials = {p.name: load_trial(p) for p in trial_dirs(root)}
    kb_dir = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb_dir, ignore=shutil.ignore_patterns("issues"))
    (kb_dir / "issues").mkdir()
    group = {
        "class": "shortcut",
        "claim": "A backup config file is left next to the live FRR config.",
        "other_claims": [],
        "environments": ["campus_lan"],
        "all_refs": [("lab__dns__h1__t01", 1)],
        "excerpts": ["frr.conf.sav"],
    }
    result = curate_group(
        group, trials, load_kb(kb_dir), kb_dir, source="test", date="2026-10-29"
    )
    assert result["written"], result
    issue = load_kb(kb_dir).issue(result["written"])
    hits = {
        "lab__dns__h1__t01": [{"issue": issue.id, "step": 1, "excerpt": "frr.conf.sav"}]
    }
    assert recorded_by(group, hits, {issue.id: "shortcut"}) == issue.id
    # Firing on the same step with unrelated text does not record the group.
    other = {
        "lab__dns__h1__t01": [{"issue": issue.id, "step": 1, "excerpt": "daemons"}]
    }
    assert recorded_by(group, other, {issue.id: "shortcut"}) is None
    # A check that identifies the instance (a state-changing command) records
    # the group by firing on its step alone.
    exact = {issue.id: "shortcut"}
    assert recorded_by(group, other, exact, frozenset(exact)) == issue.id
    # The same text signature covers a later group filed under another class.
    later = {**group, "class": "harness_defect", "excerpts": ["  frr.conf.sav  "]}
    assert covered_by_signature(later, [issue]) == issue.id


def test_cross_trial_findings_become_scoped_statistics() -> None:
    row = Row(
        "case:lab__nat__h1",
        "case",
        "lab",
        "[case:lab__nat__h1] ...",
        {"fault_types": ["nat_drop"], "confused_with": "link_down", "fault_absent": 0},
    )
    sig, scope = signature_for({"class": "label_unidentifiable"}, row)
    assert (sig["metric"], sig["pattern"], scope) == (
        "label_confusion",
        "link_down",
        {"fault_types": ["nat_drop"]},
    )
    sig, scope = signature_for({"class": "fault_not_realized"}, row)
    assert sig["metric"] == "case_fail_rate" and scope == {"fault_types": ["nat_drop"]}
    healthy = Row("healthy:lab", "healthy", "lab", "", {"scenario": "lab"})
    sig, scope = signature_for({"class": "healthy_lure"}, healthy)
    assert (sig["metric"], scope) == ("healthy_fp_rate", {"scenarios": ["lab"]})
    # The same statistic over the same scope is one issue, whatever its class.
    recorded = Issue(
        **{
            "id": "FNR-NAT",
            "title": "t",
            "class": "signal_invisible",
            "severity": "warn",
            "status": "open",
            "summary": "s",
            "scope": {"fault_types": ["nat_drop"]},
            "detect": [
                {"kind": "metric", "metric": "case_fail_rate", "threshold": 0.8}
            ],
            "provenance": {"source": "test", "date": "2026-11-01"},
        }
    )
    case_sig, case_scope = signature_for({"class": "fault_not_realized"}, row)
    assert _same(recorded, case_sig, case_scope)
