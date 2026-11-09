"""Submission audit: trace parsing, signatures, re-grade, metrics, and review tools."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import hashlib
from pathlib import Path
from typing import Any

import pytest

from experiment.submission_audit import skill

import yaml
import httpx

from nika.audit.cohort import TrialRecord, healthy_fp_rate, run_metrics
from nika.audit.cohort_audit import Row, _same, prompt_rows, signature_for
from nika.audit.cohort_audit import curate as curate_cohort
from nika.audit.cohort_audit import judge as judge_cohort
from nika.audit.pipeline import _exact, _verdicts
from nika.audit.curate import (
    common_text,
    covered_by_signature,
    curate_group,
    recorded_by,
    to_pattern,
)
from nika.audit.detectors import run_trial
from nika.audit.kb import KB_DIR, Issue, Scope, Signature, load_kb
from nika.audit.review import check_citations, check_issue, review_packet
from nika.audit.runner import audit_trajectories, prompt_contrast
from nika.audit.scan import ScanConfig
from nika.audit.scan import _parse as _parse_findings
from nika.audit.scan import chunks as scan_chunks
from nika.audit.source import SourceUnavailable, request_source, source_hash
from nika.audit.trace import load_trial, trial_dirs
from nika.audit.contracts import advertised_claims
from nika.audit.scan import scan_trials

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("server_delay", [0.1, 5.1])
def test_http_retries_share_one_wall_time_budget(monkeypatch, server_delay) -> None:
    import nika.audit.scan as scanner

    clock = [0.0]
    attempts = []
    monkeypatch.setattr(scanner.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        scanner.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )

    class Client:
        def post(self, url, **kwargs):
            attempts.append(kwargs["timeout"])
            clock[0] += server_delay
            request = httpx.Request("POST", url)
            if len(attempts) == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(
                200,
                request=request,
                json={
                    "choices": [
                        {
                            "message": {"content": '{"findings":[]}'},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {},
                },
            )

    result = scanner.chat(
        Client(),
        ScanConfig(base_url="http://test", model="test", timeout=5, retries=8),
        "system",
        "packet",
    )
    if server_delay > 5:
        assert len(attempts) == 1 and "deadline" in result["error"]
    else:
        assert len(attempts) == 2 and not result.get("error")
        assert attempts[1] < attempts[0]
    assert result["http_attempts"] == len(attempts)


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


def test_contracts_only_review_initial_capability_assertions(tmp_path: Path) -> None:
    events = [
        {
            "event": "prompt",
            "phase": "diagnosis",
            "text": "Assets are available for this task.\nDo not submit if no report is available.",
        },
        {
            "event": "prompt",
            "phase": "submission",
            "text": "The agent's report says that telemetry is available.",
        },
    ]
    path = _trial(
        tmp_path, "t__healthy__t01", events, gt=HEALTHY_GT, submission=HEALTHY_GT, rca=1
    )
    claims = advertised_claims([load_trial(path), load_trial(path)])
    assert [c["literal"] for c in claims] == ["Assets are available for this task."]


@pytest.mark.parametrize(
    "reply",
    [
        {"content": "invalid", "finish": "stop"},
        {"content": '{"findings": []}', "finish": "length"},
        {"content": '{"findings": null}', "finish": "stop"},
    ],
)
def test_bad_scan_replies_are_errors_not_empty_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: dict
) -> None:
    import nika.audit.scan as scanner

    path = _trial(
        tmp_path, "t__healthy__t01", [], gt=HEALTHY_GT, submission=HEALTHY_GT, rca=1
    )
    monkeypatch.setattr(scanner, "chat", lambda *args, **kwargs: {**reply, "sec": 0})
    result = scan_trials(
        [path], load_kb(), ScanConfig(base_url="unused", model="unused", workers=1)
    )
    assert result[0]["errors"]


def test_scan_recovers_once_and_keeps_failure_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.scan as scanner

    path = _trial(
        tmp_path, "t__healthy__t01", [], gt=HEALTHY_GT, submission=HEALTHY_GT, rca=1
    )
    replies = iter(
        [
            {
                "content": "invalid",
                "finish": "stop",
                "sec": 1,
                "prompt_tokens": 7,
                "completion_tokens": 3,
            },
            {
                "content": '{"findings": []}',
                "findings": [],
                "finish": "stop",
                "sec": 2,
                "prompt_tokens": 11,
                "completion_tokens": 5,
            },
        ]
    )
    formats = []

    def chat(*args, **kwargs):
        formats.append(kwargs.get("response_format"))
        return next(replies)

    monkeypatch.setattr(scanner, "chat", chat)
    result = scan_trials(
        [path], load_kb(), ScanConfig(base_url="unused", model="unused", workers=1)
    )[0]
    assert not result["errors"] and result["recovered_errors"] == [
        "invalid scan response"
    ]
    assert result["requests"] == 2 and result["prompt_tokens"] == 18
    assert result["failed_responses"][0]["content"] == "invalid"
    assert formats == [None, scanner.FINDINGS_FORMAT]


def test_cohort_resume_preserves_valid_tables_and_retries_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.cohort_audit as cohort

    row = Row("tool:probe", "tool", "", "probe: 10 calls, 90% errors")
    finding = {
        "class": "harness_defect",
        "row": row.id,
        "claim": "The probe errors frequently.",
        "excerpt": "90% errors",
    }
    replies = iter(
        [
            {"content": "invalid", "finish": "stop", "prompt_tokens": 5},
            {"error": "unsupported", "status_code": 400},
            {
                "content": json.dumps({"findings": [finding]}),
                "finish": "stop",
                "prompt_tokens": 7,
            },
            {"error": "deadline exceeded"},
            {"content": '{"findings":[]}', "finish": "stop"},
        ]
    )
    formats = []

    def chat(*args, **kwargs):
        formats.append(kwargs.get("response_format"))
        return next(replies)

    monkeypatch.setattr(cohort, "chat", chat)
    monkeypatch.setattr(cohort, "_NO_STRUCTURED", set())
    cfg = ScanConfig(base_url="test-cohort", model="test", workers=1)
    path = tmp_path / "scans.json"
    calls = []
    assert cohort.scan_rows(
        [row], load_kb(), cfg, cache_path=path, calls_out=calls
    ) == [finding]
    assert calls[0]["prompt_tokens"] == 12
    assert formats[0] is None and formats[1]["type"] == "json_schema"
    assert formats[2] is None
    calls.clear()
    assert cohort.scan_rows(
        [row], load_kb(), cfg, cache_path=path, calls_out=calls
    ) == [finding]
    assert len(formats) == 3 and calls[0]["cached"]
    assert calls[0]["prompt_tokens"] == 0
    changed = Row(row.id, row.kind, row.env, "probe: 10 calls, 0% errors")
    assert cohort.scan_rows([changed], load_kb(), cfg, cache_path=path) == []
    assert cohort.scan_rows([changed], load_kb(), cfg, cache_path=path) == []
    assert len(formats) == 5
    assert len(json.loads(path.read_text())) == 2
    journal = [
        json.loads(line)
        for line in (tmp_path / "scans-calls.jsonl").read_text().splitlines()
    ]
    assert len(journal) == 3
    assert journal[1]["result"]["error"] == "deadline exceeded"


def test_cohort_judgment_cache_does_not_reuse_errors_or_changed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.cohort_audit as cohort

    row = Row("tool:probe", "tool", "", "probe: 10 calls, 90% errors")
    finding = {"class": "harness_defect", "claim": "Probe errors", "row": row.id}
    replies = iter(
        [
            {"error": "deadline exceeded"},
            {
                "content": '{"verdict":"real","reason":"Repeated errors."}',
                "finish": "stop",
                "completion_tokens": 20,
            },
            {
                "content": '{"verdict":"by_design","reason":"No errors."}',
                "finish": "stop",
            },
        ]
    )
    requested = []

    def chat(*args, **kwargs):
        requested.append(args[-1])
        return next(replies)

    monkeypatch.setattr(cohort, "chat", chat)
    cfg = ScanConfig(base_url="test-cohort", model="test", workers=1)
    path = tmp_path / "judgments.json"
    assert cohort.judge([finding], [row], load_kb(), cfg, cache_path=path)[0]["error"]
    assert not path.exists()
    assert (
        cohort.judge([finding], [row], load_kb(), cfg, cache_path=path)[0]["verdict"]
        == "real"
    )
    before = path.read_bytes()
    cached = cohort.judge([finding], [row], load_kb(), cfg, cache_path=path)[0]
    assert cached["verdict"] == "real" and cached["response"]["cached"]
    assert len(requested) == 2 and before == path.read_bytes()
    changed = Row(row.id, row.kind, row.env, "probe: 10 calls, 0% errors")
    assert (
        cohort.judge([finding], [changed], load_kb(), cfg, cache_path=path)[0][
            "verdict"
        ]
        == "by_design"
    )
    assert len(requested) == 3


def test_schema_recovery_falls_back_on_endpoints_without_schema_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.scan as scanner

    path = _trial(
        tmp_path, "t__healthy__t01", [], gt=HEALTHY_GT, submission=HEALTHY_GT, rca=1
    )
    monkeypatch.setattr(scanner, "_NO_STRUCTURED", set())
    replies = iter(
        [
            {"content": "invalid", "finish": "stop", "sec": 1},
            {"error": "unsupported response format", "status_code": 400, "sec": 1},
            {"content": '{"findings":[]}', "finish": "stop", "sec": 1},
            {"content": "invalid", "finish": "stop", "sec": 1},
            {"content": '{"findings":[]}', "finish": "stop", "sec": 1},
        ]
    )
    formats = []

    def chat(*args, **kwargs):
        formats.append(kwargs.get("response_format"))
        return next(replies)

    monkeypatch.setattr(scanner, "chat", chat)
    cfg = ScanConfig(base_url="unsupported-test", model="test", workers=1)
    first = scan_trials([path], load_kb(), cfg)[0]
    second = scan_trials([path], load_kb(), cfg)[0]
    assert not first["errors"] and first["requests"] == 3
    assert not second["errors"] and second["requests"] == 2
    assert formats == [None, scanner.FINDINGS_FORMAT, None, None, None]
    assert len(first["failed_responses"]) == 2


@pytest.mark.parametrize(
    "content",
    ["invalid", "[]", '{"refs": null}', '{"refs": ["UNKNOWN"], "reason": "test"}'],
)
def test_bad_mapper_replies_fail_and_are_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    from experiment.submission_audit import map_issues as mapper

    issues = tmp_path / "kb/issues"
    issues.mkdir(parents=True)
    (issues / "ISSUE-TEST.yaml").write_text("id: ISSUE-TEST\nsummary: test\n")
    monkeypatch.setattr(
        mapper, "chat", lambda *args: {"content": content, "finish": "stop"}
    )
    cache, persisted = {}, []
    mapping, failed = mapper.map_run(
        tmp_path,
        ScanConfig(base_url="unused", model="unused", workers=1),
        "system",
        {"REF-TEST"},
        cache,
        on_result=lambda: persisted.append(dict(cache)),
    )
    assert not mapping and failed == ["ISSUE-TEST"] and not cache and not persisted
    calls = []
    monkeypatch.setattr(
        mapper,
        "chat",
        lambda *args: (
            calls.append(1)
            or {"content": '{"refs": [], "reason": "No match"}', "finish": "stop"}
        ),
    )
    assert mapper.map_run(
        tmp_path,
        ScanConfig(base_url="unused", model="unused", workers=1),
        "system",
        {"REF-TEST"},
        cache,
        on_result=lambda: persisted.append(dict(cache)),
    ) == ({"ISSUE-TEST": []}, [])
    assert persisted == [cache]
    mapper.map_run(
        tmp_path,
        ScanConfig(base_url="unused", model="unused", workers=1),
        "system",
        {"REF-TEST"},
        cache,
    )
    assert len(calls) == 1
    journal = [
        json.loads(line)
        for line in (tmp_path / "mapping-calls.jsonl").read_text().splitlines()
    ]
    assert len(journal) == 2
    assert not journal[0]["valid"] and journal[0]["response"]["content"] == content
    assert journal[1]["valid"] and journal[0]["key"] == journal[1]["key"]


@pytest.mark.parametrize("source_kind", ["source_absence", "source_text"])
def test_source_checks_require_reviewed_providers_and_report_unknowns(
    tmp_path: Path,
    source_kind: str,
) -> None:
    repo = tmp_path / "source"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "core.autocrlf", "false")
    provider = repo / "src/agent/provider.py"
    provider.parent.mkdir(parents=True)
    provider_bytes = b'ASSET_ROOT = "src/agent/assets"\r\n'
    provider.write_bytes(provider_bytes)

    def commit() -> str:
        git("add", ".")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        )
        return git("rev-parse", "HEAD")

    missing = commit()
    assert (
        source_hash(str(repo), missing, "src/agent/provider.py")
        == hashlib.sha256(provider_bytes).hexdigest()
    )
    issue = Issue(
        **{
            "id": "HARNESS-EMPTY-ASSETS",
            "title": "Empty advertised asset package",
            "class": "harness_defect",
            "severity": "info",
            "status": "open",
            "summary": "test",
            "detect": [
                {
                    "kind": source_kind,
                    "where": ["prompt"],
                    "pattern": "Skills are available",
                    "source_glob": "src/agent/assets/*/SKILL.md",
                    "source_fingerprints": [
                        {
                            "src/agent/provider.py": source_hash(
                                str(repo), missing, "src/agent/provider.py"
                            )
                        }
                    ],
                }
            ],
            "provenance": {"source": "test", "date": "2026-11-02"},
        }
    )
    kb = load_kb().model_copy(update={"issues": [issue]})
    path = _trial(
        tmp_path / "package",
        "t__healthy__t01",
        [{"event": "prompt", "phase": "diagnosis", "text": "Skills are available"}],
        gt=HEALTHY_GT,
        submission=HEALTHY_GT,
        rca=1,
    )

    def audit(commit: str, **kwargs: Any) -> dict:
        run = json.loads((path / "run.json").read_text())
        run["nika_git_commit"] = commit
        (path / "run.json").write_text(json.dumps(run))
        return audit_trajectories(path.parent.parent, kb=kb, **kwargs)

    assert audit(missing, repo=repo)["known_issues"][0]["n_trials"] == 1
    if source_kind == "source_text":
        trial = load_trial(path)
        group = {
            "excerpts": ["Skills are available"],
            "all_refs": [(trial.trial_id, 0)],
        }
        trials = {trial.trial_id: trial}
        assert covered_by_signature(group, [issue], trials, kb=kb) is None
        assert (
            covered_by_signature(group, [issue], trials, kb=kb, repo=repo) == issue.id
        )
    assert audit(missing)["unsupported_checks"][0]["n_trials"] == 1
    assert audit("unknown", repo=repo)["unsupported_checks"]
    provider.write_text('ASSET_ROOT = "src/agent/new-assets"\n')
    changed = commit()
    report = audit(changed, repo=repo)
    assert not report["known_issues"] and report["unsupported_checks"]
    if source_kind == "source_text":
        trials = {trial.trial_id: load_trial(path)}
        assert covered_by_signature(group, [issue], trials, kb=kb, repo=repo) is None
    asset = repo / "src/agent/assets/basic/SKILL.md"
    asset.parent.mkdir(parents=True)
    asset.write_text("# Basic troubleshooting\n")
    provider.write_bytes(provider_bytes)
    supplied = commit()
    report = audit(supplied, repo=repo)
    assert bool(report["known_issues"]) == (source_kind == "source_text")
    assert not report["unsupported_checks"]
    matches = request_source(
        repo, missing, {"action": "search", "path": "src/agent", "query": "ASSET_ROOT"}
    )
    assert matches["matches"][0]["path"] == "src/agent/provider.py"
    assert (
        request_source(
            repo, missing, {"action": "search", "path": "src", "query": "ASSET_ROOT"}
        )
        == matches
    )
    with pytest.raises(SourceUnavailable):
        request_source(
            repo,
            missing,
            {
                "action": "read",
                "path": "experiment/submission_audit/reference_issues.yaml",
            },
        )
    with pytest.raises(ValueError):
        Signature(
            kind="source_absence",
            pattern="available",
            source_glob="../outside",
            source_fingerprints=[{"src/provider.py": "0" * 64}],
        )
    with pytest.raises(ValueError):
        Signature(
            kind="source_absence",
            pattern="available",
            source_glob="src/*",
            source_fingerprints=[{"/outside": "not-a-hash"}],
        )


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


def test_brief_triage_uses_initial_observed_endpoints(tmp_path: Path) -> None:
    from nika.audit.brief import _check_facts, brief_claims

    path = _trial(
        tmp_path,
        "t__healthy__t01",
        [
            {
                "event": "prompt",
                "phase": "diagnosis",
                "text": "PCs: leaf_a, leaf_b\nTopology: (switch:eth0, leaf_a:eth0)",
            },
            {
                "event": "prompt",
                "phase": "submission",
                "text": "PCs: unrelated\nTopology: (a:e0, b:e0)",
            },
        ],
        gt=HEALTHY_GT,
        submission=HEALTHY_GT,
        rca=1,
    )
    trial = load_trial(path)
    claim = brief_claims([trial])[0]
    assert claim["inventory_without_topology_endpoint"] == ["leaf_b"]
    observed = [
        {
            "source_request": {"action": "read"},
            "source_result": {"path": "src/nika/provider.py"},
        }
    ]
    facts = {
        "deployment_identified": True,
        "connected_nodes": ["switch", "leaf_a", "leaf_b"],
        "observed_topology_nodes": ["switch", "leaf_a"],
        "missing_connected_nodes": ["leaf_b"],
        "source_paths": ["src/nika/provider.py"],
    }
    _check_facts(claim, facts, observed)
    _check_facts(
        claim,
        {**facts, "observed_topology_nodes": ["switch:eth0", "leaf_a:eth0"]},
        observed,
    )
    with pytest.raises(ValueError, match="endpoints differ"):
        _check_facts(
            claim,
            {**facts, "observed_topology_nodes": ["switch:eth9", "leaf_a:eth0"]},
            observed,
        )
    with pytest.raises(ValueError, match="endpoints differ"):
        _check_facts(
            claim,
            {**facts, "observed_topology_nodes": ["switch", "leaf_a", "leaf_b"]},
            observed,
        )
    with pytest.raises(ValueError, match="must have been read"):
        _check_facts(claim, facts, [])
    with pytest.raises(ValueError, match="identification flag"):
        _check_facts(claim, {**facts, "deployment_identified": 1}, observed)
    initial = next(s for s in trial.steps if s.kind == "prompt")
    initial.text += ", (switch:eth1, leaf_b:eth0)"
    assert brief_claims([trial]) == []
    initial.text = "PCs: leaf_a, leaf_b\nTopology: " + ", ".join(
        f"(switch:eth{i}, leaf_a:eth{i})" for i in range(20)
    )
    unsupported = []
    assert brief_claims([trial], unsupported=unsupported) == []
    assert len(unsupported) == 1 and "200-character" in unsupported[0]["reason"]


def test_contextual_signatures_require_the_actual_call_and_pristine_state(
    tmp_path: Path,
) -> None:
    issue = Issue(
        id="HARNESS-CONTEXT-TEST",
        title="Observed path",
        **{"class": "harness_defect"},
        severity="warn",
        status="open",
        summary="test",
        detect=[
            Signature(
                kind="text",
                pattern="host-hop",
                tool="exec_shell",
                call_pattern=r'"command"\s*:\s*"traceroute\s',
                before_mutation=True,
            )
        ],
        provenance={"source": "test", "date": "2026-11-03"},
    )
    kb = load_kb().model_copy(update={"issues": [issue]})
    for name, command, preceding, expected in [
        ("original", "traceroute -n 10.0.0.2", [], True),
        ("printed", "printf host-hop", [], False),
        (
            "changed",
            "traceroute -n 10.0.0.2",
            _claude_call(
                "change", "exec_shell", "ip route replace default via 192.0.2.1", ""
            ),
            False,
        ),
        (
            "read_only",
            "traceroute -n 10.0.0.2",
            _claude_call("read", "exec_shell", "ip route show", ""),
            True,
        ),
    ]:
        path = _trial(
            tmp_path / name,
            "case__healthy__t01",
            preceding + _claude_call("trace", "exec_shell", command, "host-hop"),
            gt=HEALTHY_GT,
            submission=HEALTHY_GT,
            rca=1,
        )
        trial = load_trial(path)
        step = next(
            s.index
            for s in trial.steps
            if s.kind == "tool_result" and s.text == "host-hop"
        )
        assert bool(run_trial(kb, trial)) == expected
        group = {"all_refs": [[trial.trial_id, step]], "excerpts": ["host-hop"]}
        assert (
            bool(covered_by_signature(group, [issue], {trial.trial_id: trial}, kb=kb))
            == expected
        )


def test_boundary_requires_source_ownership_and_runtime_interface_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.boundary as boundary

    source = {
        "src/nika/net_env/example/router.startup": "ip address add 10.1.0.1/24 dev eth0\nip address add 10.4.0.2/30 dev eth1\niptables -t nat -A POSTROUTING -o eth2 -j MASQUERADE\n",
        "src/nika/net_env/example/target.startup": "ip address add 10.2.0.2/30 dev eth0\n# ip address add 8.8.8.8/32 dev lo\n",
    }
    monkeypatch.setattr(boundary, "source_files", lambda *args: tuple(source))
    monkeypatch.setattr(
        boundary, "read_source", lambda repo, commit, path: source[path]
    )
    monkeypatch.setattr(
        boundary,
        "source_hash",
        lambda repo, commit, path: hashlib.sha256(source[path].encode()).hexdigest(),
    )
    owner = boundary._owners(str(tmp_path), "a" * 40, "example")
    assert "8.8.8.8" not in owner  # A commented command is not an assignment.
    interface = _claude_call(
        "address",
        "exec_shell",
        "ip -br addr",
        "eth0 UP 10.1.0.1/24\neth2@if1 UP 172.17.0.2/16",
    )
    interface[0]["claude_event"]["message"]["content"][0]["input"]["host_name"] = (
        "router"
    )
    output = "traceroute to 10.2.0.2 (10.2.0.2), 30 hops max\n 1 10.1.0.1 0.1 ms\n 2 172.17.0.1 0.2 ms\n 3 192.0.2.1 0.3 ms"
    trace = _claude_call("trace", "exec_shell", "traceroute -n 10.2.0.2", output)
    path = _trial(
        tmp_path,
        "case__healthy__t01",
        [
            {
                "event": "prompt",
                "phase": "diagnosis",
                "text": "A lab with router and target nodes.",
            }
        ]
        + interface
        + trace,
        gt=HEALTHY_GT,
        submission=HEALTHY_GT,
        rca=1,
        scenario="example",
    )
    trial = load_trial(path)
    trial.run["nika_git_commit"] = "a" * 40
    found, unsupported = boundary.collect_boundaries([trial], tmp_path, load_kb())
    assert len(found) == 1 and not unsupported
    assert found[0]["runtime_router_interfaces"]["gateway_interfaces"] == [
        {"interface": "eth2", "address": "172.17.0.2/16"}
    ]
    assert re.search(boundary.boundary_pattern(found[0]), output)
    changed = load_trial(path)
    changed.run["nika_git_commit"] = "a" * 40
    for step in changed.steps:
        step.text = step.text.replace("10.2.0.2", "8.8.8.8")
    assert boundary.collect_boundaries([changed], tmp_path, load_kb())[0] == []
    missing = load_trial(path)
    missing.run["nika_git_commit"] = "a" * 40
    next(
        s for s in missing.steps if s.kind == "tool_result"
    ).text = "eth0 UP 10.1.0.1/24"
    assert boundary.collect_boundaries([missing], tmp_path, load_kb())[0] == []
    no_brief = load_trial(path)
    no_brief.run["nika_git_commit"] = "a" * 40
    no_brief.steps = [s for s in no_brief.steps if s.kind != "prompt"]
    absent, unsupported = boundary.collect_boundaries([no_brief], tmp_path, load_kb())
    assert not absent and "brief unavailable" in unsupported[0]["reason"]


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


def test_prompt_evidence_records_a_scoped_relational_detector(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    base = "Diagnose the network."
    extra = "Report faulty devices by name."
    sources = []
    for name, gt, text in (
        ("lab__dns__t01", DNS_GT, base + "\n" + extra),
        ("lab__healthy__t01", HEALTHY_GT, base),
    ):
        sources.append(
            _trial(
                root,
                name,
                [{"phase": "diagnosis", "event": "prompt", "text": text}],
                gt=gt,
                submission=gt,
                rca=1.0,
            )
        )
    trials = [load_trial(p) for p in sources]
    rows = prompt_rows(trials)
    assert len(rows) == 1
    assert "Fault tasks: 1/1" in rows[0].text
    assert "Healthy tasks: 0/1" in rows[0].text
    kb_dir = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb_dir, ignore=shutil.ignore_patterns("issues"))
    (kb_dir / "issues").mkdir()
    finding = {
        "class": "shortcut",
        "row": rows[0].id,
        "claim": "Prompt reveals fault presence.",
    }
    result = curate_cohort(
        [finding],
        [{"verdict": "real", "reason": "Fault-only instruction."}],
        rows,
        load_kb(kb_dir),
        kb_dir,
        [],
        [],
        source="test",
        date="2026-11-02",
    )
    issue = load_kb(kb_dir).issue(result[0]["written"])
    assert issue.provenance.refs == [f"{t.trial_id}:0" for t in trials]
    assert issue.detect[0].kind == "prompt_contrast"
    report = audit_trajectories(root, kb=load_kb(kb_dir))
    assert (
        next(r for r in report["known_issues"] if r["id"] == issue.id)["n_trials"] == 1
    )
    # A matching fragment outside the issue's environment is not this issue.
    scoped = issue.model_copy(update={"scope": Scope(scenarios=["elsewhere"])})
    assert not audit_trajectories(
        root, kb=load_kb().model_copy(update={"issues": [scoped]})
    )["known_issues"]
    # A different contrast in the same environment must not match this pattern.
    unrelated = issue.model_copy(
        update={
            "detect": [Signature(kind="prompt_contrast", pattern="different fragment")]
        }
    )
    assert not audit_trajectories(
        root, kb=load_kb().model_copy(update={"issues": [unrelated]})
    )["known_issues"]
    # A counterfactual healthy task with the same instruction removes the cue.
    trials[1].steps[0].text += "\n" + extra
    assert prompt_rows(trials) == []
    # Deployment and code differences cannot establish a fault-presence cue.
    trials[1].steps[0].text = base
    for key in ("scenario_topo_size", "nika_git_commit"):
        trials[0].run[key] = "different"
        assert prompt_rows(trials) == []
        del trials[0].run[key]
    trials[1].steps = []
    assert prompt_rows(trials) == []
    assert prompt_contrast(trials) == ({}, set())


def test_sparse_prompt_cues_keep_repeated_support_and_healthy_controls(
    tmp_path: Path,
) -> None:
    base = "Diagnose the network."
    cue = "Use the cluster-specific troubleshooting workflow."
    for i in range(8):
        _trial(
            tmp_path,
            f"lab__dns__h{i}__t01",
            [
                {
                    "phase": "diagnosis",
                    "event": "prompt",
                    "text": base + ("\n" + cue if i < 2 else ""),
                }
            ],
            gt=DNS_GT,
            submission=DNS_GT,
            rca=1.0,
        )
    _trial(
        tmp_path,
        "lab__healthy__t01",
        [{"phase": "diagnosis", "event": "prompt", "text": base}],
        gt=HEALTHY_GT,
        submission=HEALTHY_GT,
        rca=1.0,
    )
    trials = [load_trial(p) for p in trial_dirs(tmp_path)]
    assert prompt_contrast(trials, min_share=0.5) == ({}, set())
    found, hit = prompt_contrast(trials)
    assert found == {"campus_lan": [cue]} and len(hit) == 2
    rows = prompt_rows(trials)
    assert len(rows) == 1
    kb_dir = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb_dir, ignore=shutil.ignore_patterns("issues"))
    (kb_dir / "issues").mkdir()
    written = curate_cohort(
        [
            {
                "class": "shortcut",
                "row": rows[0].id,
                "claim": "All fault tasks contain the cue.",
            }
        ],
        [{"verdict": "real", "reason": "Repeated fault-only instruction."}],
        rows,
        load_kb(kb_dir),
        kb_dir,
        [],
        [],
        source="test",
        date="2026-11-04",
    )
    issue = load_kb(kb_dir).issue(written[0]["written"])
    assert "2/8 fault trials" in issue.summary
    assert "All fault tasks" not in issue.summary
    for trial in trials:
        if trial.is_healthy_case:
            trial.steps[0].text += "\n" + cue
    assert prompt_contrast(trials) == ({}, set())
    assert prompt_rows(trials) == []
    for trial in trials:
        if trial.is_healthy_case or trial.trial_id == sorted(hit)[0]:
            trial.steps[0].text = base
    assert prompt_contrast(trials) == ({}, set())
    assert prompt_rows(trials) == []


def test_terminal_errors_remain_visible_to_the_auditor(tmp_path: Path) -> None:
    events = [
        {
            "event": "tool_start",
            "phase": "diagnosis",
            "tool": {"name": "run_probe"},
            "input": {},
            "tool_call_id": "c1",
        },
        {
            "event": "subprocess_stall",
            "phase": "diagnosis",
            "stall_s": 300,
            "reconnect_failure": False,
        },
        {"event": "agent_error", "phase": "diagnosis", "error": "diagnosis stalled"},
    ]
    path = _trial(tmp_path, "lab__dns__t01", events, gt=DNS_GT, submission={}, rca=0.0)
    run = json.loads((path / "run.json").read_text())
    run.update(
        outcome="agent_failed",
        score_status="no_submission",
        agent_error="diagnosis stalled",
    )
    (path / "run.json").write_text(json.dumps(run))
    trial = load_trial(path)
    assert [s.kind for s in trial.steps] == ["tool_call", "harness", "harness"]
    assert trial.steps[1].meta["reconnect_failure"] is False
    packet = review_packet(trial)
    assert "agent_failed" in packet and "no_submission" in packet
    assert "subprocess_stall" in packet and "diagnosis stalled" in packet


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
    repair = _claude_call(
        "c1", "mcp__kathara_base_mcp_server__exec_shell", "systemctl restart frr", ""
    )
    root = tmp_path / "pkg"
    _trial(root, "lab__dns__h1__t01", leak, gt=DNS_GT, submission=DNS_GT, rca=1.0)
    _trial(root, "lab__dns__h1__t02", repair, gt=DNS_GT, submission=DNS_GT, rca=1.0)
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
    assert issue.scope.scenarios == ["campus_lan"]
    # The learned issue records a later group with the same evidence, whatever
    # class the scan filed it under, but not outside the issue's scope.
    later = {**group, "class": "harness_defect", "excerpts": ["  frr.conf.sav  "]}
    assert covered_by_signature(later, [issue], trials, kb=load_kb(kb_dir)) == issue.id
    elsewhere = issue.model_copy(update={"scope": Scope(scenarios=["other"])})
    assert covered_by_signature(later, [elsewhere], trials, kb=load_kb(kb_dir)) is None
    # The built-in state-change check records a group citing the command.
    kb = load_kb(kb_dir)
    hits = audit_trajectories(root, kb=kb)["trials"]
    exact = {i.id: i.issue_class for i in kb.issues if _exact(i)}
    repair_group = {
        "class": "agent_integrity",
        "all_refs": [("lab__dns__h1__t02", 0)],
        "excerpts": ["systemctl restart frr"],
    }
    assert recorded_by(repair_group, hits, exact) == "AGENT-STATE-MUTATION"
    assert recorded_by({**repair_group, "class": "shortcut"}, hits, exact) is None


@pytest.mark.parametrize("restriction", ["tool", "scope", "where", "authored"])
def test_dedup_requires_the_detector_to_match_its_cited_step(tmp_path, restriction):
    paths = [
        _trial(
            tmp_path,
            f"lab__dns__t0{i}",
            _claude_call("c1", "mcp__server__exec_shell", "ls", output),
            gt=DNS_GT,
            submission=DNS_GT,
            rca=1,
        )
        for i, output in enumerate(["frr.conf.sav", "regular-file"])
    ]
    trials = {p.name: load_trial(p) for p in paths}
    tid = paths[0].name
    kb = load_kb()
    issue = Issue(
        **{
            "id": "LEAK-BACKUP",
            "title": "Backup config",
            "class": "shortcut",
            "severity": "warn",
            "status": "open",
            "summary": "A config backup is exposed.",
            "scope": {"scenarios": ["campus_lan"]},
            "detect": [{"kind": "text", "pattern": r"frr\.conf\.sav"}],
            "provenance": {"source": "test", "date": "2026-11-04"},
        }
    )
    group = {"class": "shortcut", "all_refs": [(tid, 1)], "excerpts": ["frr.conf.sav"]}
    assert covered_by_signature(group, [issue], trials, kb=kb) == issue.id
    if restriction == "scope":
        trials[tid].run["benchmark_fingerprint"] = json.dumps({"scenario": "other"})
        group["all_refs"].append((paths[1].name, 1))
    elif restriction == "authored":
        call = trials[tid].steps[0]
        call.text = json.dumps({"command": "cat frr.conf.sav"})
    else:
        update = (
            {"tool": "another_tool"}
            if restriction == "tool"
            else {"where": ["agent_text"]}
        )
        issue.detect = [issue.detect[0].model_copy(update=update)]
    assert covered_by_signature(group, [issue], trials, kb=kb) is None


@pytest.mark.parametrize(
    "mode",
    [
        "accept",
        "fragment",
        "canary",
        "incomplete",
        "invalid",
        "missing_boolean",
        "unsupported_format",
    ],
)
def test_curation_validates_uncited_outputs_and_retries_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from nika.audit import curate_validate as validator

    root = tmp_path / "pkg"
    for i in range(4):
        _trial(
            root,
            f"lab__dns__h1__t0{i}",
            _claude_call("c1", "mcp__server__help", "help", "kind {drop,loss,mtu}"),
            gt=DNS_GT,
            submission=DNS_GT,
            rca=1,
        )
    trials = {p.name: load_trial(p) for p in trial_dirs(root)}
    kb_dir = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb_dir, ignore=shutil.ignore_patterns("issues"))
    (kb_dir / "issues").mkdir()
    tid = sorted(trials)[0]
    group = {
        "class": "shortcut",
        "claim": "Help reveals the active fault.",
        "refs": [f"{tid}:1"],
        "all_refs": [(tid, 1)],
        "excerpts": ["kind {drop,loss,mtu}"],
        "environments": ["campus_lan"],
    }
    kb = load_kb(kb_dir)
    assert curate_group(group, trials, kb, kb_dir, source="test", date="2026-11-02")[
        "why"
    ].startswith("signature too generic:")
    calls = []

    def chat(*args, **kwargs):
        packet = json.loads(args[-1])
        calls.append(packet)
        schema = kwargs.get("response_format")
        if schema:
            expected = schema["json_schema"]["schema"]
            assert "portable_without_ground_truth" in expected["required"]
            assert expected["properties"]["decisions"]["minItems"] == 3
            if mode == "unsupported_format":
                return {"error": "unsupported format", "status_code": 400}
        assert [o["kind"] for o in packet["outputs"]] == [
            "observation",
            "detector_match",
            "negative_control",
        ]
        facts = {
            "condition_kind": "static_disclosure",
            "same_target": True,
            "output_sufficient": True,
            "portable_without_ground_truth": True,
            "claim": "Help lists the manipulation modes.",
            "limitation": "This proves the active fault and its diagnostic impact.",
            "decisions": [
                {"id": 0, "same_disclosure": True},
                {"id": 1, "same_disclosure": mode != "fragment"},
                {"id": 2, "same_disclosure": mode == "canary"},
            ],
        }
        if mode == "incomplete":
            facts["decisions"].pop()
        if mode in {"missing_boolean", "unsupported_format"}:
            facts["condition_kind"] = "other"
            facts["decisions"] = [{"id": i, "same_disclosure": False} for i in range(3)]
            if not schema and len(calls) == 1:
                facts.pop("portable_without_ground_truth")
        return {
            "content": "invalid" if mode == "invalid" else json.dumps(facts),
            "finish": "stop",
            "prompt_tokens": 17,
            "completion_tokens": 9,
        }

    monkeypatch.setattr(validator, "chat", chat)

    def validate():
        return validator.validate_group(
            group,
            trials,
            kb,
            kb_dir,
            tmp_path / "work",
            ScanConfig(base_url="unused", model=f"test-{mode}"),
            source="test",
            date="2026-11-02",
        )

    result = validate()
    invalid = mode in {"incomplete", "invalid"}
    repaired = mode in {"missing_boolean", "unsupported_format"}
    assert result["validation_calls"] == (
        3 if mode == "unsupported_format" else 2 if invalid or repaired else 1
    )
    assert result["validation_tokens"] == ([34, 18] if invalid or repaired else [17, 9])
    if mode == "accept":
        issue = load_kb(kb_dir).issue(result["written"])
        assert issue.title == "Help lists the manipulation modes."
        assert issue.detect[0].tool == "help"
        assert "need separate evidence" in issue.summary
        assert "This proves the active fault" not in issue.summary
    else:
        assert not result["written"] and not list((kb_dir / "issues").iterdir())
    again = validate()
    assert len(calls) == (
        3 if invalid or mode == "unsupported_format" else 2 if repaired else 1
    )
    assert again["validation_calls"] == int(invalid)
    journal = tmp_path / "work" / "validation-calls.jsonl"
    attempts = [json.loads(line) for line in journal.read_text().splitlines()]
    raw_attempts = [a for row in attempts for a in row["record"]["attempts"]]
    assert len(raw_attempts) == len(calls)
    assert all(bool(row["record"].get("error")) == invalid for row in attempts)
    assert all(
        a["response"].get("prompt_tokens") == 17
        for a in raw_attempts
        if not a["response"].get("error")
    )


def test_source_reader_unwraps_only_unambiguous_readonly_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.contracts as contracts

    replies = iter(
        [
            {"function": {"action": "search", "query": "provider", "path": "src"}},
            {"function": {"action": "read"}, "params": {"action": "search"}},
            {"action": "computer_use"},
            {"action": "finish"},
        ]
    )
    requests, observations = [], []
    monkeypatch.setattr(
        contracts,
        "chat",
        lambda *args: {"content": json.dumps(next(replies)), "finish": "stop"},
    )
    monkeypatch.setattr(
        contracts,
        "request_source",
        lambda repo, commit, request: requests.append(request) or {"matches": []},
    )
    result = contracts.read_evidence(
        None,
        {},
        "a" * 40,
        tmp_path,
        ScanConfig(base_url="unused", model="test"),
        "Read evidence",
        observations=observations,
    )
    assert result["action"] == "finish" and len(requests) == 1
    assert sum("error" in o["source_result"] for o in observations) == 2


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
    assert not _same(recorded, {**case_sig, "threshold": 0.9}, case_scope)
    assert not _same(recorded, {**case_sig, "min_trials": 10}, case_scope)
    for restricted in (
        Scope(fault_types=["nat_drop"], healthy=True),
        Scope(fault_types=["nat_drop"], backends=["kathara"]),
    ):
        assert not _same(
            recorded.model_copy(update={"scope": restricted}), case_sig, case_scope
        )


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        "[]",
        '{"verdict":"real","reason":""}',
        '{"verdict":"maybe","reason":"unsupported verdict"}',
    ],
)
def test_invalid_adjudications_are_retried_instead_of_cached(
    tmp_path: Path, monkeypatch, content: str
) -> None:
    path = _trial(
        tmp_path / "pkg",
        "lab__dns__h1__t00",
        _claude_call(
            "c1", "mcp__kathara_base_mcp_server__exec_shell", "ls", "frr.conf.sav"
        ),
        gt=DNS_GT,
        submission=DNS_GT,
        rca=1.0,
    )
    trials = {path.name: load_trial(path)}
    group = {
        "class": "shortcut",
        "claim": "Backup config",
        "refs": [f"{path.name}:1"],
        "all_refs": [(path.name, 1)],
        "trials": 1,
        "fault_trials": 1,
        "healthy_trials": 0,
        "cases": 1,
        "environments": ["campus_lan"],
    }
    replies = [
        content,
        json.dumps(
            {"verdict": "real", "reason": "Backup config appears in tool output."}
        ),
    ]
    calls = []

    def chat(*args):
        calls.append(1)
        return {"content": replies.pop(0), "finish": "stop"}

    monkeypatch.setattr("nika.audit.adjudicate.chat", chat)
    cache = tmp_path / "verdicts.json"
    old_key = json.dumps([group["class"], group["claim"], group["all_refs"]])
    cache.write_text(
        json.dumps({old_key: {"verdict": "unclear", "reason": "", "error": ""}})
    )
    cfg = ScanConfig(base_url="unused", model="test")
    first = _verdicts(cache, [group], [0], trials, load_kb(), cfg)
    assert first[0]["error"]
    assert json.loads(cache.read_text()) == {}
    assert (
        _verdicts(cache, [group], [0], trials, load_kb(), cfg)[0]["verdict"] == "real"
    )
    assert (
        _verdicts(cache, [group], [0], trials, load_kb(), cfg)[0]["verdict"] == "real"
    )
    assert len(calls) == 2
    monkeypatch.setattr(
        "nika.audit.cohort_audit.chat",
        lambda *args: {"content": content, "finish": "stop"},
    )
    finding = {"class": "shortcut", "row": "prompt:test", "claim": "Prompt cue"}
    row = Row("prompt:test", "prompt", "campus_lan", "Paired prompt evidence")
    invalid = judge_cohort([finding], [row], load_kb(), cfg)[0]
    assert invalid["error"] and not invalid["reason"]


def test_changed_evidence_invalidates_a_cached_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    path = _trial(
        tmp_path / "pkg",
        "lab__dns__t01",
        _claude_call("c1", "mcp__server__exec_shell", "ls /etc/frr", "frr.conf.sav"),
        gt=DNS_GT,
        submission=DNS_GT,
        rca=1.0,
    )
    trials = {path.name: load_trial(path)}
    group = {
        "class": "shortcut",
        "claim": "Backup config exposed",
        "refs": [f"{path.name}:1"],
        "all_refs": [(path.name, 1)],
        "trials": 1,
        "fault_trials": 1,
        "healthy_trials": 0,
        "cases": 1,
        "environments": ["campus_lan"],
    }
    replies = [
        '{"verdict":"real","reason":"Backup present."}',
        '{"verdict":"by_design","reason":"No backup in the new evidence."}',
    ]
    calls = []

    def chat(*args):
        calls.append(1)
        return {"content": replies.pop(0), "finish": "stop"}

    monkeypatch.setattr("nika.audit.adjudicate.chat", chat)
    cache = tmp_path / "verdicts.json"
    cfg = ScanConfig(base_url="unused", model="test")
    assert (
        _verdicts(cache, [group], [0], trials, load_kb(), cfg)[0]["verdict"] == "real"
    )
    assert (
        _verdicts(cache, [group], [0], trials, load_kb(), cfg)[0]["verdict"] == "real"
    )
    assert len(calls) == 1
    trials[path.name].steps[1].text = "frr.conf"
    assert (
        _verdicts(cache, [group], [0], trials, load_kb(), cfg)[0]["verdict"]
        == "by_design"
    )
    assert len(calls) == 2


def test_completed_verdict_survives_interruption_of_the_remaining_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.pipeline as pipeline

    group = {
        "class": "shortcut",
        "claim": "Candidate A",
        "refs": [],
        "trials": 1,
        "fault_trials": 1,
        "healthy_trials": 0,
        "cases": 1,
        "environments": ["lab"],
    }
    groups = [group, {**group, "claim": "Candidate B"}]
    verdict = {
        "verdict": "real",
        "reason": "Verified evidence.",
        "tokens": [3, 7],
        "error": "",
    }
    cache = tmp_path / "verdicts.json"

    def interrupted(groups, trials, kb, cfg, *, on_result):
        on_result(0, verdict)
        assert len(json.loads(cache.read_text())) == 1
        raise RuntimeError("interrupted after one completion")

    monkeypatch.setattr(pipeline, "adjudicate_groups", interrupted)
    cfg = ScanConfig(base_url="unused", model="test")
    with pytest.raises(RuntimeError):
        pipeline._verdicts(cache, groups, [0, 1], {}, load_kb(), cfg)
    assert len((tmp_path / "verdicts-calls.jsonl").read_text().splitlines()) == 1

    def resumed(groups, trials, kb, cfg, *, on_result):
        assert [g["claim"] for g in groups] == ["Candidate B"]
        on_result(0, verdict)
        return [verdict]

    monkeypatch.setattr(pipeline, "adjudicate_groups", resumed)
    assert (
        pipeline._verdicts(cache, groups, [0, 1], {}, load_kb(), cfg) == [verdict] * 2
    )


def test_adjudication_whitespace_preserves_the_explicit_verdict() -> None:
    from nika.audit.adjudicate import _verdict

    result = _verdict(
        '{"verdict":" unclear", "reason":"The tool schema is not recorded."}'
    )
    assert result == {
        "verdict": "unclear",
        "reason": "The tool schema is not recorded.",
    }
    assert _verdict('{"verdict":" possibly_real ","reason":"A claim."}').get(
        "parse_error"
    )
    assert _verdict('{"verdict":["real"],"reason":"A claim."}').get("parse_error")


def test_truncated_judgment_is_an_error_even_if_json_is_complete(monkeypatch) -> None:
    from nika.audit.adjudicate import adjudicate_groups

    monkeypatch.setattr(
        "nika.audit.adjudicate.chat",
        lambda *args: {
            "content": '{"verdict":"real","reason":"A claim."}',
            "finish": "length",
        },
    )
    group = {
        "class": "shortcut",
        "claim": "Candidate",
        "refs": [],
        "trials": 1,
        "fault_trials": 1,
        "healthy_trials": 0,
        "cases": 1,
        "environments": ["lab"],
    }
    result = adjudicate_groups(
        [group], {}, load_kb(), ScanConfig(base_url="unused", model="test")
    )[0]
    assert result["error"] and result["verdict"] == "unclear" and not result["reason"]


def test_measured_controls_refine_a_signature_and_prevent_false_dedup(
    tmp_path: Path,
) -> None:
    from nika.audit.curate import observation_capsule

    output = "alpha localhost$\nbeta controller$\ngamma worker"
    root = tmp_path / "pkg"
    for i in range(4):
        _trial(
            root,
            f"lab__dns__t0{i}",
            _claude_call("c1", "mcp__server__exec_shell", "cat -A /etc/hosts", output),
            gt=DNS_GT,
            submission=DNS_GT,
            rca=1,
        )
    trials = {f.name: load_trial(f) for f in trial_dirs(root)}
    tid = sorted(trials)[0]
    kbdir = skill.builtin_kb_dir(tmp_path / "kb")
    kb = load_kb(kbdir)
    group = {
        "class": "healthy_lure",
        "claim": "The final entry is ignored by the resolver.",
        "refs": [f"{tid}:1"],
        "all_refs": [(tid, 1)],
        "excerpts": [output],
        "environments": ["campus_lan"],
    }
    draft = curate_group(
        group,
        trials,
        kb,
        kbdir,
        source="unit fixture",
        date="2026-11-03",
        min_share=0,
        write=False,
    )
    old = Issue(**draft["proposal"])
    group.update(
        confirmation=[{"request": {"fixture": "measured file-normalization pair"}}],
        counterfactuals=[{"trial": tid, "step": 1, "text": output + "$\n"}],
    )
    assert covered_by_signature(group, [old], trials, kb=kb) is None
    result = curate_group(
        group, trials, kb, kbdir, source="unit fixture", date="2026-11-03"
    )
    issue = load_kb(kbdir).issue(result["written"])
    assert (
        issue.detect[0].pattern.startswith(r"\A")
        and issue.detect[0].tool == "exec_shell"
    )
    assert issue.scope.scenarios == ["campus_lan"] and issue.scope.releases == ["0.2.0"]
    single = kb.model_copy(update={"issues": [issue]})
    assert all(run_trial(single, t) for t in trials.values())
    assert not run_trial(
        single, observation_capsule(trials[tid], 1, output + "$\n", kb.policy)
    )
    assert covered_by_signature(group, [issue], trials, kb=kb) == issue.id
    call = trials[tid].steps[0]
    call.text = json.dumps({**json.loads(call.text), "host_name": "pc1"})
    group["confirmation_bindings"] = [
        {"host": "pc1", "providers": {"src/nika/first.py": "a" * 64}},
        {"host": "other", "providers": {"src/nika/second.py": "b" * 64}},
    ]
    proposal = curate_group(
        group, trials, kb, kbdir, source="test", date="2026-11-04", write=False
    )
    signatures = Issue(**proposal["proposal"]).detect
    assert len(signatures) == 2
    for sig, binding in zip(signatures, group["confirmation_bindings"], strict=True):
        assert sig.source_fingerprints == [binding["providers"]]
        for host in ("pc1", "other"):
            call = json.dumps({"host_name": host, "command": "cat -A /etc/hosts"})
            assert bool(sig.call_regex.search(call)) == (host == binding["host"])

    call = trials[tid].steps[0]
    call.text = json.dumps({**json.loads(call.text), "command": "cat  -A /etc/hosts"})
    rejected = curate_group(
        group, trials, kb, kbdir, source="test", date="2026-11-04", write=False
    )
    assert not rejected.get("proposal") and "cited steps" in rejected["why"]


def test_automatic_file_probe_requires_matching_source_and_unterminated_last_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nika.audit.normalization as normalization

    raw = "127.0.0.1 localhost\n192.0.2.1 earlier\n192.0.2.2 last"
    events = _claude_call(
        "c1",
        "mcp__server__exec_shell",
        "cat -A /etc/hosts",
        normalization._display(raw),
    )
    events[0]["claude_event"]["message"]["content"][0]["input"]["host_name"] = "router"
    path = _trial(
        tmp_path,
        "lab__healthy__t01",
        events,
        gt=HEALTHY_GT,
        submission=HEALTHY_GT,
        rca=1,
    )
    trial = load_trial(path)
    trial.run["nika_git_commit"] = "a" * 40
    group = {
        "class": "healthy_lure",
        "claim": "A parser may skip the last record.",
        "all_refs": [(trial.trial_id, 1)],
    }
    trials = {trial.trial_id: trial}
    source = [raw]
    monkeypatch.setattr(normalization, "read_source", lambda *args: source[0])
    args = ([group], trials, load_kb(), [], tmp_path)
    found = normalization.candidates(*args)
    assert len(found) == 1
    assert found[0]["source_path"] == "src/nika/net_env/campus_lan/router/etc/hosts"
    assert found[0]["groups"] == [0]
    for changed in (raw + "\n", raw.replace("last", "another"), raw + "\n# comment"):
        source[0] = changed
        assert not normalization.candidates(*args)
    source[0] = raw
    trial.steps[1].text = "an agent's unsupported claim"
    assert not normalization.candidates(*args)
    assert not normalization._names("192.0.2.1 same\n192.0.2.2 same")


def test_stale_source_navigation_falls_back_to_acquisition(tmp_path, monkeypatch):
    import nika.audit.brief as brief
    from nika.audit.kb import SourceRead

    for invalid in (
        {"path": "../private", "start": 1, "end": 2},
        {"path": "src/nika/provider.py", "start": 1, "end": 181},
        {"action": "write", "path": "src/nika/provider.py", "start": 1, "end": 2},
    ):
        with pytest.raises(ValueError):
            SourceRead(**invalid)
    claim = {
        "commit": "a" * 40,
        "prompt": "Topology: (a:e0, b:e0)",
        "literal": "Topology: (a:e0, b:e0)",
    }
    read = {"action": "read", "path": "src/nika/provider.py", "start": 1, "end": 2}
    commits = []
    monkeypatch.setattr(
        brief,
        "request_source",
        lambda repo, commit, request: (
            commits.append(commit)
            or {"path": request["path"], "lines": ["1: changed source"]}
        ),
    )
    facts = {
        "deployment_identified": False,
        "connected_nodes": [],
        "observed_topology_nodes": ["a", "b"],
        "missing_connected_nodes": [],
        "source_paths": [read["path"]],
    }
    recovered = {
        **facts,
        "deployment_identified": True,
        "connected_nodes": ["a", "b", "c"],
        "missing_connected_nodes": ["c"],
    }
    replies = iter(
        [
            facts,
            recovered,
            {
                "verdict": "real",
                "reason": "The observed summary omits a connected node.",
            },
        ]
    )
    monkeypatch.setattr(
        brief,
        "chat",
        lambda *args: {"content": json.dumps(next(replies)), "finish": "stop"},
    )
    acquisitions = []

    def acquire(*args, **kwargs):
        acquisitions.append(1)
        return {"action": "finish"}

    monkeypatch.setattr(brief, "read_evidence", acquire)
    result = brief.inspect_brief(
        claim,
        tmp_path,
        ScanConfig(base_url="unused", model="unused"),
        source_reads=[read],
    )
    assert result["source_plan_used"] and result["source_plan_fallback"]
    assert acquisitions == [1] and commits == [claim["commit"]]
    assert result["facts"] == recovered and result["judgment"]["verdict"] == "real"


def test_source_facts_recover_one_outer_object_without_accepting_conflicting_answers():
    from nika.audit.contracts import _object

    facts = {"selected_node_uses_image": True, "details": {"provider": "source.py"}}
    reply = {
        "finish": "stop",
        "content": 'Code uses {"image": IMAGE}.\n```json\n'
        + json.dumps(facts)
        + "\n```",
    }
    assert _object(reply) == facts
    for invalid in (
        {**reply, "finish": "length"},
        {
            **reply,
            "content": json.dumps(facts)
            + json.dumps({"selected_node_uses_image": False}),
        },
    ):
        with pytest.raises(ValueError):
            _object(invalid)


@pytest.mark.parametrize("stream_end", ["complete", "eof", "disconnect"])
def test_reasoning_stream_collects_answer_and_usage_but_rejects_partial_stream(
    monkeypatch, stream_end
):
    import nika.audit.scan as scan

    monkeypatch.setattr(scan, "_NO_STREAM", set())
    requests = []
    events = [
        {"choices": [{"delta": {"reasoning_content": "internal analysis"}}]},
        {"choices": [{"delta": {"content": '{"verdict":'}}]},
        {"choices": [{"delta": {"content": '"real"}'}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7}},
    ]
    wire = "".join("data: " + json.dumps(x) + "\n\n" for x in events)
    if stream_end == "complete":
        wire += "data: [DONE]\n\n"

    def handle(request):
        requests.append(json.loads(request.content))
        if stream_end == "disconnect":

            class Disconnected(httpx.SyncByteStream):
                def __iter__(self):
                    yield wire.encode()
                    raise httpx.RemoteProtocolError("gateway disconnected")

            return httpx.Response(200, stream=Disconnected())
        return httpx.Response(
            200, content=wire, headers={"content-type": "text/event-stream"}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = scan.chat(
            client,
            ScanConfig(base_url="http://test", model="test", thinking=True, retries=2),
            "system",
            "packet",
        )
    assert requests[0]["stream"] is True
    assert requests[0]["chat_template_kwargs"]["enable_thinking"] is True
    if stream_end != "complete":
        assert result["error"] and "content" not in result
    else:
        assert result["content"] == '{"verdict":"real"}' and result["finish"] == "stop"
        assert result["prompt_tokens"] == 11 and result["completion_tokens"] == 7
    assert result["http_attempts"] == len(requests) == 1


def test_streaming_fallback_preserves_compatible_endpoints_and_counts_requests(
    monkeypatch,
):
    import nika.audit.scan as scan

    monkeypatch.setattr(scan, "_NO_STREAM", set())
    modes = []
    monkeypatch.setenv("OPENAI_API_KEY", "audit-test-token")

    def handle(request):
        assert request.headers["Authorization"] == "Bearer audit-test-token"
        payload = json.loads(request.content)
        modes.append(payload.get("stream", False))
        if payload.get("stream"):
            return httpx.Response(
                400, json={"error": {"message": "stream_options is unsupported"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}]
            },
        )

    cfg = ScanConfig(base_url="http://test", model="test", thinking=True, retries=0)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        first = scan.chat(client, cfg, "system", "packet")
        second = scan.chat(client, cfg, "system", "packet")
    assert first["content"] == second["content"] == "answer"
    assert first["http_attempts"] == 2 and second["http_attempts"] == 1
    assert modes == [True, False, False]


def test_streaming_checks_deadline_and_closes_the_response(monkeypatch):
    import nika.audit.scan as scan

    clock, closed = [0.0], []
    monkeypatch.setattr(scan, "_NO_STREAM", set())
    monkeypatch.setattr(scan.time, "monotonic", lambda: clock[0])

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 6.0
            yield b'data: {"choices":[]}\n\n'

        def close(self):
            closed.append(True)

    def handle(request):
        return httpx.Response(200, stream=Stream())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = scan.chat(
            client,
            ScanConfig(
                base_url="http://test",
                model="test",
                thinking=True,
                retries=0,
                timeout=5,
            ),
            "system",
            "packet",
        )
    assert result["error"] and result["http_attempts"] == 1 and closed


def test_capture_proof_requires_both_controls_exact_source_and_matching_binary():
    import copy
    from nika.audit.capture_probe import CapturePlan, confirms_start_failure

    plan = {
        "image": "image@sha256:" + "a" * 64,
        "commit": "b" * 40,
        "source_path": "src/nika/capture.py",
        "interface": "ethernet-1/1",
    }
    CapturePlan(**plan)
    for wrong in (
        {**plan, "image": "image:latest"},
        {**plan, "interface": "lo"},
        {**plan, "interface": "rpcap://remote/eth0"},
    ):
        with pytest.raises(ValueError):
            CapturePlan(**wrong)
    live = {
        "returned": {"capture_backend": "dumpcap"},
        "child_state": "S",
        "pcap_magic": "0a0d0d0a",
        "pcap_exists": True,
    }
    dead = {
        "returned": {"capture_backend": "dumpcap"},
        "child_state": "Z",
        "pcap_exists": False,
        "backend_log": "No such device exists",
    }
    receipt = {
        "complete": True,
        "resources_removed": True,
        "request": {"plan": plan, "source_sha256": "a" * 64},
        "measurement": {"dumpcap_version": ["version"], "conditions": []},
    }
    for label, sha, outcome in [
        ("recorded", "a" * 64, dead),
        ("readiness-fixed", "b" * 64, {"raised": "RuntimeError('No such device')"}),
    ]:
        receipt["measurement"]["conditions"].append(
            {
                "label": label,
                "source_sha256": sha,
                "observations": [
                    {
                        "role": "valid",
                        "interface": "lo",
                        "outcome": copy.deepcopy(live),
                    },
                    {
                        "role": "observed",
                        "interface": plan["interface"],
                        "outcome": outcome,
                    },
                ],
            }
        )
    assert confirms_start_failure(receipt, "version")
    assert not confirms_start_failure(receipt, "another version")
    for field in ("valid", "observed", "source", "cleanup"):
        changed = copy.deepcopy(receipt)
        if field == "valid":
            changed["measurement"]["conditions"][0]["observations"][0]["outcome"] = dead
        if field == "observed":
            changed["measurement"]["conditions"][0]["observations"][1]["outcome"] = live
        if field == "source":
            changed["measurement"]["conditions"][0]["source_sha256"] = "c" * 64
        if field == "cleanup":
            changed["resources_removed"] = False
        assert not confirms_start_failure(changed, "version")


def test_runtime_scopes_do_not_transfer_to_other_or_unknown_backends(tmp_path):
    from nika.audit.detectors import in_scope

    path = _trial(
        tmp_path, "lab__healthy__t01", [], gt=HEALTHY_GT, submission=HEALTHY_GT, rca=1
    )
    trial = load_trial(path)
    issue = (
        load_kb()
        .issues[0]
        .model_copy(update={"scope": Scope(backends=["containerlab"])})
    )
    for backend, expected in [
        ("containerlab", True),
        ("kathara", False),
        (None, False),
    ]:
        trial.run["backend"] = backend
        assert in_scope(issue, trial) is expected


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_capture_source_repair_is_bounded_and_charges_both_attempts(
    tmp_path, monkeypatch, repair_succeeds
):
    from nika.audit import capture

    candidate = {
        "commit": "a" * 40,
        "device": "router",
        "declared_nodes": ["router"],
        "node_identity": {"capabilities": ["linux"]},
        "scenario_params": {},
        "scenario": "deployment",
    }
    monkeypatch.setattr(capture, "source_packet", lambda c: {"commit": c["commit"]})
    monkeypatch.setattr(capture, "request_source", lambda *args: {"matches": []})
    budgets, answers = [], []

    def read(*args, **kwargs):
        budgets.append(kwargs["max_requests"])
        kwargs["calls"].append({"content": "source request"})
        return {"action": "finish"}

    def answer(*args, **kwargs):
        answers.append(True)
        facts = {"resolved": repair_succeeds and len(answers) > 1}
        return {"content": json.dumps(facts), "finish": "stop"}

    def check(record, *args):
        if not record["facts"]["resolved"]:
            raise ValueError("imported image definition is missing")

    monkeypatch.setattr(capture, "read_evidence", read)
    monkeypatch.setattr(capture, "chat", answer)
    monkeypatch.setattr(capture, "_check_source", check)
    cfg = ScanConfig(base_url="unused", model="test")
    result = capture.inspect_source(candidate, tmp_path, cfg, tmp_path)
    assert budgets == [6, 2]
    assert len(result["calls"]) == 4
    assert bool(result.get("error")) is not repair_succeeds
    assert result["prior_acquisition"]["error"]
    if repair_succeeds:
        assert capture.inspect_source(candidate, tmp_path, cfg, tmp_path)["cached"]
        assert len(answers) == 2


def test_frr_backup_evidence_does_not_label_a_dhcp_reference(tmp_path):
    from experiment.submission_audit import scan_eval

    _trial(
        tmp_path,
        "enterprise_branch__healthy__t01",
        _claude_call(
            "backup",
            "mcp__kathara_base_mcp_server__exec_shell",
            "ls /etc/frr",
            "daemons\nfrr.conf\nfrr.conf.sav\nvtysh.conf",
        ),
        gt=HEALTHY_GT,
        submission=HEALTHY_GT,
        rca=1,
        scenario="enterprise_branch",
    )
    aliases = yaml.safe_load((scan_eval.HERE / "issue_refs.yaml").read_text())
    found = scan_eval.labels(tmp_path, aliases)
    assert "REF-FRR-SAV" in found
    assert "REF-DHCP-LEASES-BACKUP" not in found


@pytest.mark.parametrize(
    "requested_server", ["frr_mcp_server", "kathara_frr_mcp_server"]
)
def test_missing_tool_evidence_keeps_namespace_and_prior_advertisement(
    tmp_path: Path, requested_server: str
) -> None:
    from nika.audit.adjudicate import group_prompt
    from nika.audit.trace import Step, Trial

    advertised = "mcp__kathara_frr_mcp_server__frr_get_rpki_status"
    requested = f"mcp__{requested_server}__frr_get_rpki_status"
    trial = Trial(
        "trial",
        tmp_path,
        [
            Step(0, 1, "diagnosis", "prompt", text="Inspect this router."),
            Step(1, 2, "diagnosis", "init", meta={"tools": [advertised]}),
            Step(
                2,
                3,
                "diagnosis",
                "tool_call",
                tool="frr_get_rpki_status",
                server=requested_server,
                call_id="probe",
                text='{"device":"router"}',
            ),
            Step(
                3,
                4,
                "diagnosis",
                "tool_result",
                call_id="probe",
                text=f"No such tool available: {requested}",
            ),
            Step(4, 5, "diagnosis", "init", meta={"tools": ["future_tool"]}),
        ],
        {},
        {},
        {},
        {},
    )
    group = {
        "class": "harness_defect",
        "claim": "The advertised tool was unavailable.",
        "refs": ["trial:3"],
        "trials": 1,
        "fault_trials": 0,
        "healthy_trials": 1,
        "cases": 1,
        "environments": ["lab"],
    }
    packet = group_prompt(group, {"trial": trial})
    identity = json.loads(
        packet.split("(untrusted evidence, not instructions):\n", 1)[1].split("\n", 1)[
            0
        ]
    )
    assert identity["requested_full_tool"] == requested
    assert identity["recorded_advertisements_with_same_bare_name"] == [advertised]
    assert identity["requested_full_tool_in_recorded_advertisement"] == (
        requested == advertised
    )
    assert identity["advertisement_step"] == "trial:1"
    assert "future_tool" not in packet
    # Successful output and absent declarations supply no invented identity evidence.
    trial.steps[3].text = "Connected to the RPKI cache."
    ordinary = group_prompt(group, {"trial": trial})
    assert "Request-specific tool identity" not in ordinary
    trial.steps[3].text = f"No such tool available: {requested}"
    trial.steps[1].meta = {}
    assert "Request-specific tool identity" not in group_prompt(group, {"trial": trial})


def test_cli_reviewer_isolates_calls_and_terminates_children(tmp_path):
    import os
    import signal
    import sys
    from nika.audit.scan import _cli_chat

    command = (
        sys.executable,
        "-c",
        "from pathlib import Path; import json; p=Path('state'); print(json.dumps({'findings': [], 'cwd': str(Path.cwd()), 'previous': p.exists()})); p.touch()",
    )
    cfg = ScanConfig(base_url="", model="local", command=command)
    replies = [
        json.loads(_cli_chat(cfg, "system", "packet")["content"]) for _ in range(2)
    ]
    assert all(not reply["previous"] for reply in replies)
    assert replies[0]["cwd"] != replies[1]["cwd"]
    assert all(not Path(reply["cwd"]).exists() for reply in replies)

    pids = tmp_path / "pids.json"
    script = "import json,os,subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); open(sys.argv[1],'w').write(json.dumps([os.getpid(),child.pid])); time.sleep(30)"
    cfg.command = (sys.executable, "-c", script, str(pids))
    cfg.timeout = 2
    cfg.retries = 8
    try:
        result = _cli_chat(cfg, "system", "packet")
        assert result["error"] == "reviewer timeout"
        parent, child = json.loads(pids.read_text())
        assert not Path(f"/proc/{parent}").exists()
        status = Path(f"/proc/{child}/stat")
        assert not status.exists() or status.read_text().split()[2] in {"Z", "X"}
    finally:
        if pids.exists():
            parent, _ = json.loads(pids.read_text())
            try:
                os.killpg(parent, signal.SIGKILL)
            except ProcessLookupError:
                pass
