"""The shipped audit knowledge base loads and its references resolve."""

from __future__ import annotations

import subprocess

import pytest

from nika.audit.cohort import METRICS
from nika.audit.kb import load_kb
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
    """Regression detection compares ``fixed_in`` with a run's commit by ancestry."""
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
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", issue.fixed_in, "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
        )
        assert ancestor.returncode == 0, (
            f"{issue.id}: fixed_in {issue.fixed_in} is not in HEAD's history"
        )
