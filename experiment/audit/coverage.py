"""Coverage of a published benchmark release against full-audit results.

A release case with no live audit is ``not_run``. That status is a coverage
gap and does not admit the case.
"""

from __future__ import annotations

from typing import Any

from experiment.audit.environment import (
    CaseAudit,
    admission_status,
    identity_from_row,
)
from nika.workflows.benchmark.release import load_release
from experiment.audit.provenance import provenance_complete


def release_cases(version: str = "0.2.0") -> list[dict[str, Any]]:
    """Return dev and test rows for a frozen release, split tagged."""
    rows: list[dict[str, Any]] = []
    for split in ("dev", "test"):
        release = load_release(version, split=split)
        for row in release.cases:
            item = dict(row)
            item["split"] = split
            rows.append(item)
    return rows


def cover_release(
    rows: list[dict[str, Any]], audits: list[CaseAudit]
) -> list[dict[str, Any]]:
    """Match live audits to release rows. Unmatched rows stay ``not_run``.

    A case audited on several fresh labs passes only when every repeat passes.
    """
    by_key: dict[str, list[CaseAudit]] = {}
    for audit in audits:
        if provenance_complete(audit.provenance):
            by_key.setdefault(audit.identity.key(), []).append(audit)
    covered: list[dict[str, Any]] = []
    for row in rows:
        identity = identity_from_row(row)
        declared_probe = str(row.get("symptom_probe") or "")
        runs = [
            audit
            for audit in by_key.get(identity.key(), [])
            if not declared_probe or audit.symptom_probe == declared_probe
        ]
        if not runs:
            status = "not_run"
            stages: list[dict[str, Any]] = []
            probe = declared_probe
        else:
            status = admission_status([audit.admission() for audit in runs])
            stages = [stage.model_dump() for stage in runs[0].stages]
            probe = runs[0].symptom_probe or declared_probe
        covered.append(
            {
                "split": row.get("split") or "",
                "identity": identity,
                "symptom_probe": probe,
                "admission": status,
                "repeats": len(runs),
                "repeats_passed": sum(audit.admission() == "pass" for audit in runs),
                "stages": stages,
            }
        )
    return covered


def gap_count(covered: list[dict[str, Any]]) -> dict[str, int]:
    """Count admission statuses. ``pass`` is the only admitted bucket."""
    counts = {
        "pass": 0,
        "fail": 0,
        "skipped": 0,
        "unsupported": 0,
        "no_evidence": 0,
        "not_run": 0,
    }
    for row in covered:
        status = str(row["admission"])
        counts[status] = counts.get(status, 0) + 1
    counts["cases"] = len(covered)
    counts["admitted"] = counts["pass"]
    return counts
