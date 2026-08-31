"""Local validation for leaderboard submission packages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from nika.config import REPO_ROOT
from nika.workflows.benchmark.release import ReleaseError, load_release
from nika.workflows.benchmark.trials import expand_trials
from nika.workflows.leaderboard.aggregate import (
    aggregate_trial_results,
    build_rca_confusion,
    metrics_nearly_equal,
)
from nika.workflows.leaderboard.meta_input import MetaInputError, parse_metadata_payload
from nika.workflows.leaderboard.schema import (
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    PRIMARY_METRIC,
    RCA_CONFUSION_FILENAME,
    README_FILENAME,
    RESULTS_DIRNAME,
    SCHEMA_VERSION,
    TRIAL_RESULT_FILENAME,
    TRIALS_DIRNAME,
    AggregatedMetrics,
    PackageIdentity,
    RcaConfusion,
    TrialResult,
)
from nika.workflows.leaderboard.secrets import scan_package_dir


@dataclass
class ValidationReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise LeaderboardValidateError("; ".join(self.errors))


class LeaderboardValidateError(ValueError):
    """Leaderboard submission failed local validation."""


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


def validate_leaderboard_submission(
    submission_dir: str | Path,
) -> ValidationReport:
    """Validate a leaderboard submission package. Returns a report (does not raise)."""
    errors: list[str] = []
    warnings: list[str] = []
    root = _resolve_submission_dir(submission_dir)

    if not root.is_dir():
        return ValidationReport(ok=False, errors=[f"submission dir not found: {root}"])

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
        trials_root,
    ):
        if required == trials_root:
            if not trials_root.is_dir():
                errors.append(f"missing directory: {RESULTS_DIRNAME}/{TRIALS_DIRNAME}/")
        elif not required.is_file():
            rel = required.relative_to(root).as_posix()
            errors.append(f"missing file: {rel}")
    if errors:
        return ValidationReport(ok=False, errors=errors)

    try:
        raw_meta = _load_yaml(metadata_path)
        if not isinstance(raw_meta, dict):
            raise MetaInputError(f"{METADATA_FILENAME} must be a YAML object")
        parse_metadata_payload(raw_meta)
    except (MetaInputError, ValidationError, yaml.YAMLError) as exc:
        return ValidationReport(
            ok=False, errors=[f"invalid {METADATA_FILENAME}: {exc}"]
        )

    try:
        identity = PackageIdentity.model_validate(_load_yaml(identity_path))
    except ValidationError as exc:
        return ValidationReport(
            ok=False,
            errors=[f"invalid {RESULTS_DIRNAME}/{IDENTITY_FILENAME}: {exc}"],
        )

    if identity.schema_version != SCHEMA_VERSION:
        errors.append(
            f"unsupported schema_version {identity.schema_version!r}; "
            f"expected {SCHEMA_VERSION!r}"
        )

    try:
        metrics = AggregatedMetrics.model_validate(_load_json(metrics_path))
    except ValidationError as exc:
        errors.append(f"invalid {RESULTS_DIRNAME}/{METRICS_FILENAME}: {exc}")
        metrics = None

    try:
        confusion = RcaConfusion.model_validate(_load_json(confusion_path))
    except ValidationError as exc:
        errors.append(f"invalid {RESULTS_DIRNAME}/{RCA_CONFUSION_FILENAME}: {exc}")
        confusion = None

    trial_results: list[TrialResult] = []
    seen_ids: set[str] = set()
    for trial_dir_path in sorted(p for p in trials_root.iterdir() if p.is_dir()):
        result_path = trial_dir_path / TRIAL_RESULT_FILENAME
        if not result_path.is_file():
            errors.append(f"missing {result_path.relative_to(root).as_posix()}")
            continue
        try:
            trial = TrialResult.model_validate(_load_json(result_path))
        except ValidationError as exc:
            errors.append(f"invalid trial result {trial_dir_path.name}: {exc}")
            continue
        if trial.trial_id != trial_dir_path.name:
            errors.append(
                f"trial directory name {trial_dir_path.name!r} != "
                f"result.trial_id {trial.trial_id!r}"
            )
        if trial.trial_id in seen_ids:
            errors.append(f"duplicate trial_id: {trial.trial_id}")
        seen_ids.add(trial.trial_id)
        trial_results.append(trial)

    # Benchmark identity vs in-tree release.
    try:
        release = load_release(
            identity.benchmark.version,
            split=identity.benchmark.split,
        )
    except ReleaseError as exc:
        errors.append(f"benchmark release load failed: {exc}")
        release = None

    if release is not None:
        checks = [
            ("id", identity.benchmark.id, release.id),
            ("version", identity.benchmark.version, release.version),
            ("split", identity.benchmark.split, release.split),
            ("case_count", identity.benchmark.case_count, release.case_count),
            ("n_trials", identity.benchmark.n_trials, release.n_trials),
        ]
        for label, left, right in checks:
            if left != right:
                errors.append(
                    f"benchmark.{label} mismatch: submission has {left!r}, "
                    f"release has {right!r}"
                )
        scoring = release.scoring if isinstance(release.scoring, dict) else {}
        expected_primary = str(scoring.get("leaderboard_primary") or PRIMARY_METRIC)
        if identity.benchmark.leaderboard_primary != expected_primary:
            errors.append(
                "benchmark.leaderboard_primary mismatch: "
                f"submission has {identity.benchmark.leaderboard_primary!r}, "
                f"release has {expected_primary!r}"
            )
        expected_scoring_id = str(scoring.get("id") or "")
        if identity.benchmark.scoring_id != expected_scoring_id:
            errors.append(
                "benchmark.scoring_id mismatch: "
                f"submission has {identity.benchmark.scoring_id!r}, "
                f"release has {expected_scoring_id!r}"
            )

        expected_trials = expand_trials(release.cases, release.n_trials)
        expected_ids = {t.trial_id for t in expected_trials}
        present_ids = {t.trial_id for t in trial_results}
        missing = sorted(expected_ids - present_ids)
        extra = sorted(present_ids - expected_ids)
        if missing:
            errors.append(f"missing trials ({len(missing)}): {missing[:5]}")
        if extra:
            errors.append(f"unexpected trials ({len(extra)}): {extra[:5]}")

        if metrics is not None and not missing and not extra:
            recomputed = aggregate_trial_results(
                trial_results, n_trials_expected=len(expected_trials)
            )
            errors.extend(metrics_nearly_equal(recomputed, metrics))

        if confusion is not None and not missing and not extra:
            recomputed_confusion = build_rca_confusion(trial_results)
            if recomputed_confusion.model_dump(mode="json") != confusion.model_dump(
                mode="json"
            ):
                errors.append(
                    f"{RESULTS_DIRNAME}/{RCA_CONFUSION_FILENAME} does not match "
                    "trial RCA labels"
                )

    if not identity.run.official:
        errors.append("run.official must be true for leaderboard submissions")

    if identity.benchmark.leaderboard_primary != PRIMARY_METRIC:
        warnings.append(
            f"non-default leaderboard_primary "
            f"{identity.benchmark.leaderboard_primary!r}"
        )

    errors.extend(scan_package_dir(root))

    return ValidationReport(ok=not errors, errors=errors, warnings=warnings)
