"""The shipped audit knowledge base loads and its references resolve."""

from __future__ import annotations

import pytest

from experiment.submission_audit import skill  # noqa: F401  (adds auditlib to sys.path)

from auditlib.cohort import METRICS
from auditlib.kb import load_kb
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
