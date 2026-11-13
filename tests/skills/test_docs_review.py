"""The documentation gate requires a consistent review of selected files."""

import json

import pytest

from scripts.review_docs import letta_quota_exhausted, main, review_verdict

pytestmark = pytest.mark.unit


def test_incomplete_review_fails_without_inventing_a_document_finding():
    assert (
        review_verdict(
            {
                "verdict": "FAIL",
                "summary": "Could not read the checkout.",
                "findings": [],
            },
            ["docs/README.md"],
        )
        == "FAIL"
    )


def test_optional_feedback_passes_and_blocking_feedback_fails():
    report = {
        "verdict": "PASS",
        "summary": "Instructions match the implementation.",
        "findings": [
            {
                "file": "docs/README.md",
                "line": 1,
                "severity": "optional",
                "reason": "Shorten the heading.",
            }
        ],
    }
    assert review_verdict(report, ["docs/README.md"]) == "PASS"
    report["findings"][0]["severity"] = "blocking"
    with pytest.raises(ValueError, match="disagrees"):
        review_verdict(report, ["docs/README.md"])
    report["verdict"] = "FAIL"
    assert review_verdict(report, ["docs/README.md"]) == "FAIL"


@pytest.mark.parametrize(
    "field,value",
    [("file", "unselected.md"), ("severity", "ignored"), ("line", 0), ("reason", "")],
)
def test_invalid_findings_cannot_pass(field, value):
    finding = {
        "file": "docs/README.md",
        "line": 1,
        "severity": "optional",
        "reason": "Shorten the heading.",
    }
    finding[field] = value
    with pytest.raises(ValueError):
        review_verdict(
            {"verdict": "PASS", "summary": "Review", "findings": [finding]},
            ["docs/README.md"],
        )


def test_only_quota_errors_select_the_fallback_reviewer():
    review = [
        {"type": "system", "subtype": "init", "agent_id": "agent-1"},
        {"type": "assistant", "content": "Note the usage_limit_reached wording."},
        {"type": "result", "subtype": "success", "is_error": False},
    ]
    assert not letta_quota_exhausted(review)
    crash = [{"type": "error", "message": "Connection reset", "stop_reason": "error"}]
    assert not letta_quota_exhausted(crash)
    quota = {
        "type": "error",
        "message": "LLM API error",
        "stop_reason": "llm_api_error",
        "api_error": {"detail": "Error code: 429 - usage_limit_reached"},
    }
    assert letta_quota_exhausted([*review[:2], quota])


def test_hosted_report_publishes_the_same_comment_as_the_fallback(
    tmp_path, monkeypatch
):
    report = {
        "verdict": "FAIL",
        "summary": "One command is wrong.",
        "findings": [
            {
                "file": "docs/README.md",
                "line": 3,
                "severity": "blocking",
                "reason": "Fix the command.",
            }
        ],
    }
    (tmp_path / "report.json").write_text(json.dumps(report))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_FILES", json.dumps(["docs/README.md"]))
    monkeypatch.setenv("REVIEW_COMMENT", str(tmp_path / "comment.md"))
    monkeypatch.setattr("sys.argv", ["review_docs.py", "report", "report.json"])
    main()
    assert (tmp_path / ".docs-review-verdict").read_text() == "FAIL\n"
    comment = (tmp_path / "comment.md").read_text()
    assert "line 3: Fix the command." in comment and "hosted Letta reviewer" in comment
    monkeypatch.setattr("sys.argv", ["review_docs.py", "report", "missing.json"])
    with pytest.raises(SystemExit, match="missing or invalid"):
        main()
