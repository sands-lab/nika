"""The local documentation gate requires a consistent review of selected files."""

import pytest

from scripts.review_docs import review_verdict

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
