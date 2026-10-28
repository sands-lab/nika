"""Local validation for leaderboard submission packages."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from nika.config import REPO_ROOT
from nika.evaluator.validity import DEFAULT_MAX_HARNESS_RATE, validity_findings
from nika.workflows.benchmark.official_release import ReleaseSource
from nika.workflows.benchmark.release import BenchmarkRelease
from nika.workflows.benchmark.trials import expand_trials
from nika.workflows.leaderboard.aggregate import (
    aggregate_trial_results,
    build_rca_confusion,
    metrics_nearly_equal,
)
from nika.workflows.leaderboard.issues import (
    IssueCollector,
    LeaderboardValidateError,
    ValidationIssue,
    ValidationReport,
)
from nika.workflows.leaderboard.meta_input import MetaInputError, parse_metadata_payload
from nika.workflows.leaderboard.release_check import (
    coverage_issues,
    load_reference,
    run_commit_changes,
)
from nika.workflows.leaderboard.schema import (
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    PRIMARY_METRIC,
    RCA_CONFUSION_FILENAME,
    README_FILENAME,
    RESULTS_DIRNAME,
    TRIAL_RESULT_FILENAME,
    TRIALS_DIRNAME,
    AggregatedMetrics,
    PackageIdentity,
    RcaConfusion,
    TrialResult,
)
from nika.workflows.leaderboard.secrets import scan_package_dir

__all__ = [
    "LeaderboardValidateError",
    "ValidationReport",
    "validate_leaderboard_submission",
]


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_submission_dir(path: str | Path) -> Path:
    root = Path(path)
    if not root.is_absolute():
        root = (REPO_ROOT / root).resolve()
    else:
        root = root.resolve()
    return root


def _failed(code: str, item: str) -> ValidationReport:
    return ValidationReport(issues=[ValidationIssue(code, items=[item])])


def benchmark_identity_mismatches(
    identity: PackageIdentity, release: BenchmarkRelease
) -> list[str]:
    """Describe benchmark identity fields that differ from the release."""
    scoring = release.scoring if isinstance(release.scoring, dict) else {}
    checks = [
        ("id", identity.benchmark.id, release.id),
        ("version", identity.benchmark.version, release.version),
        ("split", identity.benchmark.split, release.split),
        ("case_count", identity.benchmark.case_count, release.case_count),
        ("n_trials", identity.benchmark.n_trials, release.n_trials),
        (
            "leaderboard_primary",
            identity.benchmark.leaderboard_primary,
            str(scoring.get("leaderboard_primary") or PRIMARY_METRIC),
        ),
        ("scoring_id", identity.benchmark.scoring_id, str(scoring.get("id") or "")),
    ]
    return [
        f"benchmark.{label} mismatch: submission has {left!r}, release has {right!r}"
        for label, left, right in checks
        if left != right
    ]


def validate_leaderboard_submission(
    submission_dir: str | Path,
    *,
    max_harness_rate: float = DEFAULT_MAX_HARNESS_RATE,
    release_source: ReleaseSource = "official",
) -> ValidationReport:
    """Validate a leaderboard submission package. Returns a report (does not raise).

    Expected trials come from the official release (the ``release/<version>``
    tag); ``release_source="local"`` reads this checkout instead, for offline
    development. Run-validity flags warn; ``truncated_final_turn`` or
    ``infra_error`` above ``max_harness_rate`` of trials fails, since those are
    harness faults.
    """
    issues = IssueCollector()
    root = _resolve_submission_dir(submission_dir)

    if not root.is_dir():
        return _failed("ERROR_PACKAGE_INCOMPLETE", f"submission dir not found: {root}")

    metadata_path = root / METADATA_FILENAME
    readme_path = root / README_FILENAME
    results_root = root / RESULTS_DIRNAME
    identity_path = results_root / IDENTITY_FILENAME
    metrics_path = results_root / METRICS_FILENAME
    confusion_path = results_root / RCA_CONFUSION_FILENAME
    trials_root = results_root / TRIALS_DIRNAME

    for required in (
        metadata_path,
        readme_path,
        identity_path,
        metrics_path,
        confusion_path,
    ):
        if not required.is_file():
            rel = required.relative_to(root).as_posix()
            issues.add("ERROR_PACKAGE_INCOMPLETE", f"missing file: {rel}")
    if not trials_root.is_dir():
        issues.add(
            "ERROR_PACKAGE_INCOMPLETE",
            f"missing directory: {RESULTS_DIRNAME}/{TRIALS_DIRNAME}/",
        )
    if issues.has_errors:
        return ValidationReport(issues=issues.issues)

    try:
        raw_meta = _load_yaml(metadata_path)
        if not isinstance(raw_meta, dict):
            raise MetaInputError(f"{METADATA_FILENAME} must be a YAML object")
        parse_metadata_payload(raw_meta)
    except (MetaInputError, ValidationError, yaml.YAMLError) as exc:
        return _failed("ERROR_METADATA_INVALID", f"invalid {METADATA_FILENAME}: {exc}")

    try:
        identity = PackageIdentity.model_validate(_load_yaml(identity_path))
    except ValidationError as exc:
        return _failed(
            "ERROR_IDENTITY_INVALID",
            f"invalid {RESULTS_DIRNAME}/{IDENTITY_FILENAME}: {exc}",
        )

    try:
        metrics = AggregatedMetrics.model_validate(_load_json(metrics_path))
    except ValidationError as exc:
        issues.add(
            "ERROR_RESULTS_INVALID",
            f"invalid {RESULTS_DIRNAME}/{METRICS_FILENAME}: {exc}",
        )
        metrics = None

    try:
        confusion = RcaConfusion.model_validate(_load_json(confusion_path))
    except ValidationError as exc:
        issues.add(
            "ERROR_RESULTS_INVALID",
            f"invalid {RESULTS_DIRNAME}/{RCA_CONFUSION_FILENAME}: {exc}",
        )
        confusion = None

    trial_results: list[TrialResult] = []
    seen_ids: set[str] = set()
    for trial_dir_path in sorted(p for p in trials_root.iterdir() if p.is_dir()):
        result_path = trial_dir_path / TRIAL_RESULT_FILENAME
        if not result_path.is_file():
            issues.add(
                "ERROR_PACKAGE_INCOMPLETE",
                f"missing {result_path.relative_to(root).as_posix()}",
            )
            continue
        try:
            trial = TrialResult.model_validate(_load_json(result_path))
        except ValidationError as exc:
            issues.add(
                "ERROR_RESULTS_INVALID",
                f"invalid trial result {trial_dir_path.name}: {exc}",
            )
            continue
        if trial.trial_id != trial_dir_path.name:
            issues.add(
                "ERROR_RESULTS_INVALID",
                f"trial directory name {trial_dir_path.name!r} != "
                f"result.trial_id {trial.trial_id!r}",
            )
        if trial.trial_id in seen_ids:
            issues.add("ERROR_RESULTS_INVALID", f"duplicate trial_id: {trial.trial_id}")
        seen_ids.add(trial.trial_id)
        trial_results.append(trial)

    release, reference = load_reference(
        identity.benchmark.version,
        identity.benchmark.split,
        release_source,
        issues,
    )

    if release is not None and reference is not None:
        issues.extend(
            "ERROR_BENCHMARK_MISMATCH", benchmark_identity_mismatches(identity, release)
        )

        expected_trials = expand_trials(release.cases, release.n_trials)
        coverage = coverage_issues(
            release,
            reference,
            expected_trials,
            {t.trial_id for t in trial_results},
            issues,
            changed_tasks=run_commit_changes(
                release, reference, identity.run.nika_git_commit, issues
            ),
        )
        complete = not (
            coverage.drifted_tasks or coverage.missing or coverage.unexpected
        )

        if metrics is not None and complete:
            recomputed = aggregate_trial_results(
                trial_results, n_trials_expected=len(expected_trials)
            )
            issues.extend(
                "ERROR_SCORE_MISMATCH", metrics_nearly_equal(recomputed, metrics)
            )
            status_counts = recomputed.status_counts or {}
            n_grading = int(status_counts.get("grading_error") or 0)
            n_infra = int(status_counts.get("infra_error") or 0)
            if n_grading:
                issues.add(
                    "ERROR_GRADING_ERROR",
                    f"grading_error trials forbidden in submission (found {n_grading})",
                )
            v_errors, v_warnings = validity_findings(
                recomputed.validity_counts,
                len(trial_results),
                max_harness_rate=max_harness_rate,
            )
            issues.extend("ERROR_RUN_VALIDITY", v_errors)
            issues.extend("WARNING_RUN_VALIDITY", v_warnings)
            if n_infra:
                issues.add(
                    "ERROR_INFRA_ERROR",
                    f"residual infra_error trials forbidden in submission "
                    f"(found {n_infra}; re-run until none remain)",
                )

        if confusion is not None and complete:
            recomputed_confusion = build_rca_confusion(trial_results)
            if recomputed_confusion.model_dump(mode="json") != confusion.model_dump(
                mode="json"
            ):
                issues.add(
                    "ERROR_SCORE_MISMATCH",
                    f"{RESULTS_DIRNAME}/{RCA_CONFUSION_FILENAME} does not match "
                    "trial RCA labels",
                )

    if not identity.run.official:
        issues.add(
            "ERROR_UNOFFICIAL_RUN",
            "run.official must be true for leaderboard submissions",
        )

    if identity.benchmark.leaderboard_primary != PRIMARY_METRIC:
        issues.add(
            "WARNING_NON_DEFAULT_PRIMARY",
            f"non-default leaderboard_primary {identity.benchmark.leaderboard_primary!r}",
        )

    issues.extend("ERROR_SECRET_FOUND", scan_package_dir(root))

    return ValidationReport(issues=issues.issues, reference=reference)
