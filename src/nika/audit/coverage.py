"""Coverage of a published benchmark release against full-audit results.

A release case with no live audit is ``not_run``. That status is a coverage
gap. ``admits`` rejects it.
"""

from __future__ import annotations

from typing import Any

from nika.audit.environment import (
    CaseAudit,
    identity_from_row,
)
from nika.workflows.benchmark.release import load_release


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
    """Match live audits to release rows. Unmatched rows stay ``not_run``."""
    by_key = {audit.identity.key(): audit for audit in audits}
    covered: list[dict[str, Any]] = []
    for row in rows:
        identity = identity_from_row(row)
        audit = by_key.get(identity.key())
        declared_probe = str(row.get("symptom_probe") or "")
        if (
            audit is not None
            and declared_probe
            and audit.symptom_probe != declared_probe
        ):
            audit = None
        if (
            audit is not None
            and declared_probe == "artifact_only"
            and audit.method_version < 2
        ):
            audit = None
        if audit is None:
            status = "not_run"
            stages: list[dict[str, Any]] = []
            probe = str(row.get("symptom_probe") or "")
        else:
            status = audit.admission()
            stages = [stage.model_dump() for stage in audit.stages]
            probe = audit.symptom_probe or str(row.get("symptom_probe") or "")
        covered.append(
            {
                "split": row.get("split") or "",
                "identity": identity,
                "symptom_probe": probe,
                "admission": status,
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
