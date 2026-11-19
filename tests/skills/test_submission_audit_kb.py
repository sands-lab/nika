"""The shipped audit knowledge base loads and its references resolve."""

from __future__ import annotations

import subprocess
import json
import re

import pytest

from nika.audit.cohort import METRICS
from nika.audit.kb import load_kb
from nika.audit.runner import _fix_is_ancestor
from nika.config import REPO_ROOT
from nika.mcp.registry import MCP_SERVER_SPECS

pytestmark = pytest.mark.contract


def test_knowledge_base_loads_with_known_classes_and_metrics() -> None:
    kb = load_kb()
    assert kb.issues
    for issue in kb.issues:
        for sig in issue.detect:
            if sig.kind == "metric":
                assert sig.metric in METRICS, f"{issue.id}: unknown metric {sig.metric}"


def test_policy_allows_every_nika_mcp_server() -> None:
    policy = load_kb().policy
    for name in MCP_SERVER_SPECS:
        assert policy.server_regex.fullmatch(name), name


def test_fixed_in_commits_are_in_checkout_history() -> None:
    """Every fixed issue has a fix in the checked-out history."""
    shallow = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        pytest.skip("needs a full Git history")
    for issue in load_kb().issues:
        if not issue.fixed_in:
            continue
        commits = [issue.fixed_in, issue.fixed_in_equivalent]
        assert any(
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
            ).returncode
            == 0
            for commit in commits
            if commit
        ), f"{issue.id}: no fix is in HEAD's history: {commits}"


def test_regression_detection_across_equivalent_histories(tmp_path) -> None:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-qm", "original fix")
    original = git("rev-parse", "HEAD")
    git("checkout", "-q", "--detach", base)
    git("commit", "--allow-empty", "-qm", "equivalent fix")
    equivalent = git("rev-parse", "HEAD")

    issue = (
        load_kb()
        .issue("HARNESS-ORACLE-PACED-PROBE-TIMEOUT")
        .model_copy(update={"fixed_in": original, "fixed_in_equivalent": equivalent})
    )
    assert _fix_is_ancestor(issue, base, tmp_path) is False
    assert _fix_is_ancestor(issue, original, tmp_path) is True
    assert _fix_is_ancestor(issue, equivalent, tmp_path) is True
    issue.fixed_in = "0" * 40
    assert _fix_is_ancestor(issue, equivalent, tmp_path) is True
    assert _fix_is_ancestor(issue, base, tmp_path) is None


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("The output of your last command was too long", True),
        (
            "<warning>\nThe output of your last tool call exceeded the output limit "
            "and was truncated.\n</warning>\n<output_head>\n{}",
            True,
        ),
        ('{"ok": true, "summary": {"leaf_1": {"ipv4_lpm": 11}}}', False),
    ],
)
def test_truncation_policy_recognizes_live_gateway_outputs(output, expected):
    assert bool(load_kb().policy.truncated_regex.search(output)) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('File "/opt/nika/p4rt_manager.py", line 1239', True),
        ('File "/opt/p4rt/p4rt_manager.py", line 1239', False),
        ("", False),
    ],
)
def test_branding_requires_a_benchmark_marker(output, expected):
    issue = load_kb().issue("LEAK-HARNESS-PATHS-IN-OUTPUT")
    assert any(re.search(sig.pattern, output) for sig in issue.detect) is expected


@pytest.mark.parametrize(
    "example",
    json.loads(
        (REPO_ROOT / "tests/fixtures/audit/live_issue_signatures.json").read_text()
    ),
    ids=lambda example: example["id"],
)
def test_live_issue_signatures_match_observations_and_reject_empty_output(example):
    issue = load_kb().issue(example["id"])
    assert any(re.search(sig.pattern, example["output"]) for sig in issue.detect)
    assert not any(re.search(sig.pattern, "") for sig in issue.detect)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("eth0 inet 10.1.10.2/8\n++ ip addr add 10.1.10.2/24 dev eth0", True),
        ("+ ip -4 address replace 192.0.2.2/25 dev eth0", True),
        ("eth0 inet 10.1.10.2/8\nlease=none", False),
        ("ip addr add 10.1.10.2/24 dev eth0", False),
    ],
)
def test_pre_fault_copy_lead_recognizes_startup_address_trace(output, expected):
    issue = load_kb().issue("LEAK-PRE-FAULT-STATE-COPY")
    trace = next(sig for sig in issue.detect if sig.tool == "^exec_shell$")
    assert trace.where == ["tool_result"]
    assert bool(re.search(trace.pattern, output)) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("1970-01-01 00:00:00.000 INVALID  Ignore 6 10.0.0.1:1", True),
        ("2026-11-17 23:35:55.358 0.005 6 10.0.0.1:1", False),
        ("", False),
    ],
)
def test_nfdump_default_format_detector(output, expected):
    issue = load_kb().issue("HARNESS-NFDUMP-DEFAULT-FORMAT")
    rule = issue.detect[0]
    assert rule.tool == "^nfdump$"
    assert rule.where == ["tool_result"]
    assert bool(re.search(rule.pattern, output)) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('{"next_offset": 100, "omitted_rows": 20}', True),
        ('{"next_offset": null, "omitted_rows": 15}', True),
        ('{"next_offset": 25, "omitted_rows": 0}', False),
        ('{"next_offset": 25}', False),
    ],
)
def test_truncated_pagination_detector(output, expected):
    issue = load_kb().issue("HARNESS-TRUNCATED-PAGINATION-SKIP")
    rule = issue.detect[0]
    assert rule.tool == "^flow_coverage$"
    assert bool(re.search(rule.pattern, output, re.S)) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("no_sibling_in_the_real_subnet", True),
        ("no_baseline_observer", True),
        ("healthy control passed", False),
    ],
)
def test_netmask_missing_local_control_detector(output, expected):
    issue = load_kb().issue("HARNESS-NETMASK-NO-LOCAL-CONTROL")
    rule = issue.detect[0]
    assert rule.where == ["harness"]
    assert bool(re.search(rule.pattern, output)) is expected
