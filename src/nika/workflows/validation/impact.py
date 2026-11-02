"""Evaluate fault impact against authored requirements in each observation scope."""

from collections import Counter
from collections.abc import Iterable

from nika.net_env.contract import ValidationReport


def assess_fault_impact(
    reports: Iterable[ValidationReport],
    *,
    intent_ids: Iterable[str],
    expected_failed_ids: Iterable[str],
    unconstrained_ids: Iterable[str] = (),
) -> dict:
    """Require declared failures and preserve every other constrained intent.

    Callers supply this stage's declared intent IDs. Unobserved or unsupported
    predicates fail acceptance, including predicates with unconstrained outcomes.
    """
    reports = tuple(reports)
    scope = set(intent_ids)
    mandatory = set(expected_failed_ids) & scope
    unconstrained = set(unconstrained_ids) & scope
    if mandatory & unconstrained:
        raise ValueError("Mandatory and unconstrained fault intents must be disjoint")
    preserved = scope - mandatory - unconstrained
    results = [row for report in reports for row in report.results]
    counts = Counter(row.intent for row in results)
    failed = {row.intent for row in results if row.status == "failed"}
    missing = scope - counts.keys()
    unknown = counts.keys() - scope
    duplicate = {intent for intent, count in counts.items() if count != 1}
    unresolved = {
        row.intent for row in results if row.status not in {"passed", "failed"}
    }
    sanity_failed = [
        {
            "contract_id": report.contract_id,
            "check": check.check,
            "status": check.status,
        }
        for report in reports
        for check in report.sanity
        if check.status != "passed"
    ]
    violations = {
        "missing_required_failures": sorted(mandatory - failed),
        "unexpected_failures": sorted(failed & preserved),
        "unresolved_ids": sorted(unresolved),
        "missing_ids": sorted(missing),
        "unknown_ids": sorted(unknown),
        "duplicate_ids": sorted(duplicate),
        "sanity_failures": sanity_failed,
    }
    return {
        "status": "failed" if any(violations.values()) else "passed",
        "must_fail_ids": sorted(mandatory),
        "must_preserve_ids": sorted(preserved),
        "unconstrained_ids": sorted(unconstrained),
        "failed_ids": sorted(failed),
        "violations": violations,
    }
