"""Check a package or result directory against the official release.

A trial id embeds a hash of its task definition, so a run made from another copy
of the release shows the same task stem with a different hash. Those tasks are
reported once as ``ERROR_RELEASE_DRIFT`` instead of as missing and unexpected
trials.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from nika.workflows.benchmark.official_release import (
    OfficialReleaseUnavailable,
    ReleaseReference,
    ReleaseSource,
    load_reference_release,
    load_release_at,
    release_drift,
    release_tag,
)
from nika.workflows.benchmark.release import BenchmarkRelease, ReleaseError
from nika.workflows.benchmark.trials import Trial, parse_task_selector
from nika.workflows.leaderboard.issues import IssueCollector, ValidationIssue

_HASH_SUFFIX_RE = re.compile(r"__h[0-9a-f]{12}$")
# ``case_key_for_row`` keeps this many readable characters before the hash.
_STEM_CHARS = 28


def task_stem(task_id: str) -> str:
    """Readable scenario/problem prefix shared by every version of a task id."""
    return _HASH_SUFFIX_RE.sub("", task_id)[:_STEM_CHARS].rstrip("_-")


@dataclass(frozen=True)
class TrialCoverage:
    drifted_tasks: list[str]
    missing: list[str]
    unexpected: list[str]


def compare_trial_ids(
    expected: list[Trial],
    present_ids: set[str],
    *,
    changed_tasks: list[str] | None = None,
) -> TrialCoverage:
    """Split trial-id differences into drifted tasks, missing, and unexpected trials.

    A task counts as drifted when none of its trials are present and the package
    has unexpected trials with the same task stem, or when ``changed_tasks``
    (from comparing task definitions directly) lists it.
    """
    expected_ids = {trial.trial_id for trial in expected}
    expected_tasks = {trial.case_key for trial in expected}
    present_tasks = {parse_task_selector(tid)[0] for tid in present_ids}
    unexpected_ids = present_ids - expected_ids

    unexpected_by_stem: dict[str, set[str]] = defaultdict(set)
    for trial_id in unexpected_ids:
        task_id = parse_task_selector(trial_id)[0]
        if task_id not in expected_tasks:
            unexpected_by_stem[task_stem(task_id)].add(trial_id)

    drifted = set(changed_tasks or ()) & expected_tasks
    explained: set[str] = set()
    for task_id in expected_tasks - present_tasks:
        stem = task_stem(task_id)
        if unexpected_by_stem.get(stem):
            drifted.add(task_id)
            explained |= unexpected_by_stem[stem]

    missing = sorted(
        trial.trial_id
        for trial in expected
        if trial.trial_id not in present_ids and trial.case_key not in drifted
    )
    return TrialCoverage(
        drifted_tasks=sorted(drifted),
        missing=missing,
        unexpected=sorted(unexpected_ids - explained),
    )


def load_reference(
    version: str,
    split: str,
    source: ReleaseSource,
    issues: IssueCollector,
) -> tuple[BenchmarkRelease | None, ReleaseReference | None]:
    """Load the release to check against, recording why it failed if it did."""
    try:
        release, reference = load_reference_release(version, split=split, source=source)
    except ReleaseError as exc:
        issues.append(
            ValidationIssue(
                "ERROR_RELEASE_UNAVAILABLE",
                summary=f"release {version} ({split}) could not be read",
                details=[str(exc)],
            )
        )
        return None, None
    if reference.source == "local":
        issues.add("WARNING_LOCAL_RELEASE")
    return release, reference


def run_commit_changes(
    release: BenchmarkRelease,
    reference: ReleaseReference,
    commit: str | None,
    issues: IssueCollector,
) -> list[str]:
    """Return official task ids defined differently at the run's NIKA commit.

    This catches changes that keep a task id, such as edited ground truth.
    """
    if reference.source != "official" or not commit or commit == reference.commit:
        return []
    try:
        at_commit = load_release_at(
            reference.version, commit, split=release.split, repo=reference.repo
        )
    except (OfficialReleaseUnavailable, ReleaseError) as exc:
        issues.append(
            ValidationIssue("WARNING_RUN_COMMIT_UNVERIFIED", details=[str(exc)])
        )
        return []
    drift = release_drift(release, at_commit)
    return drift.changed_tasks


def coverage_issues(
    release: BenchmarkRelease,
    reference: ReleaseReference,
    expected: list[Trial],
    present_ids: set[str],
    issues: IssueCollector,
    *,
    changed_tasks: list[str] | None = None,
) -> TrialCoverage:
    """Record drift, missing, and unexpected trials; return the classification."""
    coverage = compare_trial_ids(expected, present_ids, changed_tasks=changed_tasks)
    if coverage.drifted_tasks:
        issues.append(release_drift_issue(release, reference, coverage.drifted_tasks))
    issues.extend("ERROR_TRIALS_MISSING", coverage.missing)
    issues.extend("ERROR_TRIALS_UNEXPECTED", coverage.unexpected)
    return coverage


def release_drift_issue(
    release: BenchmarkRelease,
    reference: ReleaseReference,
    changed_tasks: list[str],
) -> ValidationIssue:
    version = reference.version
    tag = reference.tag or release_tag(version)
    label = "official release" if reference.source == "official" else "release"
    return ValidationIssue(
        "ERROR_RELEASE_DRIFT",
        items=list(changed_tasks),
        summary=(
            f"task definitions differ from {label} {version} "
            f"({len(changed_tasks)} tasks)"
        ),
        details=[f"Checked against: {reference.describe()}"],
        fix=(
            f"Check out the release tag (`git fetch --tags && git checkout {tag}`), "
            "re-run the changed tasks into the same result directory with "
            f"`nika benchmark run --release {version} --split {release.split} "
            "--result_dir <dir> --no-resume --task-id <task> …`, and pack again."
        ),
    )
