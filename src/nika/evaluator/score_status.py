"""Trial score_status taxonomy and submission-first resolution.

Decision order (one question first: is there ``submission.json``?):

- Has submission → grade normally (``scored``) or ``grading_error`` on GT/scorer failure.
- No submission → ``infra_error`` when ground truth is missing (env/inject never
  completed) or when there is clear infra evidence; else ``no_submission``.

Cleanup-phase infra after a successful submit does not override grading.
Ambiguous post-inject failures default to ``no_submission``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

ScoreStatus = Literal["scored", "no_submission", "infra_error", "grading_error"]

SCORE_STATUSES: tuple[ScoreStatus, ...] = (
    "scored",
    "no_submission",
    "infra_error",
    "grading_error",
)

SCORE_KEYS = (
    "detection_score",
    "localization_accuracy",
    "localization_precision",
    "localization_recall",
    "localization_f1",
    "rca_accuracy",
    "rca_precision",
    "rca_recall",
    "rca_f1",
    "fault_type_precision",
    "fault_type_recall",
    "fault_type_f1",
)

# Narrow allowlist: clear infra / LLM / MCP failures only.
_INFRA_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"rate.?limit",
        r"429\b",
        r"authentication|unauthorized|401\b|403\b",
        r"api.?key",
        r"openai|anthropic|azure.?openai",
        r"connection.?reset|connection.?refused|connection.?error",
        r"timed?\s*out.*(?:llm|api|mcp|http)",
        r"(?:llm|api|mcp|http).*timed?\s*out",
        r"mcp",
        r"fastmcp",
        r"deploy(?:ment)?\s+failed",
        r"lab\s+(?:deploy|start|failed)",
        r"docker\s+(?:error|daemon|failed)",
        r"kathara",
        r"containerlab",
        r"kubernetes|k3s|kubectl",
        r"ENOMEM|out of memory|cannot allocate",
        r"no space left on device",
        r"network unreachable|name.?resolution|dns",
        r"SSLError|CertificateError|TLS",
        r"RemoteProtocolError|httpx\.|aiohttp",
        r"ProviderError|APIConnectionError|APIStatusError",
        r"overloaded|service unavailable|503\b|502\b|500\b",
    )
)


# Absolute filesystem paths embed case ids (e.g. ``campus_lan__dns_record_error``)
# that would otherwise trip keyword patterns such as ``dns`` or ``kathara``.
_PATH_RE = re.compile(r"(?<![\w:/])/[^\s'\"]+")


def zero_scores() -> dict[str, float]:
    return {key: 0.0 for key in SCORE_KEYS}


def null_scores() -> dict[str, None]:
    return {key: None for key in SCORE_KEYS}


def scores_for_status(status: ScoreStatus) -> dict[str, float | None]:
    if status == "scored":
        raise ValueError("scored trials carry computed metric values, not placeholders")
    if status == "no_submission":
        return dict(zero_scores())
    return dict(null_scores())


def is_infra_error_evidence(error: BaseException | str | None) -> bool:
    """Return True only when the failure text clearly indicates infra / LLM / MCP."""
    if error is None:
        return False
    text = _PATH_RE.sub(" ", str(error)).strip()
    if not text:
        return False
    return any(pat.search(text) for pat in _INFRA_PATTERNS)


def resolve_score_status(
    *,
    has_submission: bool,
    grading_failed: bool = False,
    infra_evidence: bool = False,
    has_ground_truth: bool = True,
) -> ScoreStatus:
    """Submission-first score_status resolution.

    Missing ground truth with no submission means env/inject never completed,
    which is treated as ``infra_error`` even without an ``agent_error`` string.
    """
    if has_submission:
        return "grading_error" if grading_failed else "scored"
    if not has_ground_truth or infra_evidence:
        return "infra_error"
    return "no_submission"


def infer_score_status_from_artifacts(
    *,
    has_submission: bool,
    agent_error: str | None = None,
    run_meta: dict[str, Any] | None = None,
    grading_failed: bool = False,
    has_ground_truth: bool = True,
) -> ScoreStatus:
    """Infer status for live finalize from session artifacts."""
    meta = run_meta or {}
    err = agent_error if agent_error is not None else meta.get("agent_error")
    infra = is_infra_error_evidence(err if isinstance(err, str) else None)
    return resolve_score_status(
        has_submission=has_submission,
        grading_failed=grading_failed,
        infra_evidence=infra,
        has_ground_truth=has_ground_truth,
    )


def outcome_for_score_status(
    status: ScoreStatus, *, has_submission: bool
) -> Literal["success", "agent_failed"]:
    if status in ("scored", "grading_error") and has_submission:
        return "success"
    return "agent_failed"
