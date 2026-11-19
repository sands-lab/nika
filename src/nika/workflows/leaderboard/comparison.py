"""Shared configuration checks and Dev/Test summaries for leaderboard consumers."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from nika.workflows.benchmark.network_identity import MAIN_SLICES
from nika.workflows.benchmark.official_release import (
    ReleaseSource,
    load_reference_release,
)
from nika.workflows.leaderboard.aggregate import (
    aggregate_trial_results,
    score_for_primary,
)
from nika.workflows.leaderboard.schema import (
    PackageIdentity,
    SubmissionMetadata,
    TrialResult,
)


def package_configuration(package: Path) -> dict[str, Any]:
    """Read comparison fields without including host paths, dates, or run IDs."""
    identity = PackageIdentity.model_validate(
        yaml.safe_load((package / "results/identity.yaml").read_text())
    )
    metadata = SubmissionMetadata.model_validate(
        yaml.safe_load((package / "metadata.yaml").read_text())
    )
    agent = metadata.agent.model_dump(mode="json")
    agent.pop("tags")
    agent["extra"].pop("comparison_id", None)
    run = identity.run
    return {
        "benchmark_version": identity.benchmark.version,
        "scoring_id": identity.benchmark.scoring_id,
        "n_trials": identity.benchmark.n_trials,
        "agent": agent,
        **{
            k: getattr(run, k)
            for k in (
                "nika_git_commit",
                "agent_type",
                "model",
                "llm_provider",
                "max_steps",
                "reasoning_effort",
                "case_timeout_sec",
                "comparison_config",
            )
        },
    }


def configuration_mismatches(package: Path, reference: Path) -> list[str]:
    """Compare one helper's package to the maintainer's reference package."""
    actual, expected = package_configuration(package), package_configuration(reference)
    return [
        f"{key}: expected {expected[key]!r}, got {actual[key]!r}"
        for key in expected
        if actual[key] != expected[key]
    ]


def summarize_split_packages(
    packages: list[Path], *, release_source: ReleaseSource = "official"
) -> dict[str, Any]:
    """Export task-weighted split columns and slices using NIKA aggregation.

    Call after package validation. A comparison has at most one package per split.
    Dev and Test remain independently packable official release runs.
    """
    if not packages:
        raise ValueError("At least one scores package is required")
    split_trials = {}
    split_rows = {}
    all_trials = []
    seen = set()
    identity = None
    for package in packages:
        mismatches = configuration_mismatches(package, packages[0])
        if mismatches:
            raise ValueError("Run configuration mismatch: " + "; ".join(mismatches))
        identity = PackageIdentity.model_validate(
            yaml.safe_load((package / "results/identity.yaml").read_text())
        )
        trials = [
            TrialResult.model_validate_json(p.read_text())
            for p in sorted((package / "results/trials").glob("*/result.json"))
        ]
        parts = defaultdict(list)
        for trial in trials:
            split = (
                trial.dataset.get("dataset_split")
                if identity.benchmark.split == "dev+test"
                else identity.benchmark.split
            )
            if split not in {"dev", "test"}:
                raise ValueError(
                    "Combined packages require dataset_split on every trial"
                )
            parts[split].append(trial)
            if trial.trial_id in seen:
                raise ValueError(f"Task overlap across Dev and Test: {trial.trial_id}")
            seen.add(trial.trial_id)
        for split, part in parts.items():
            if split in split_trials:
                raise ValueError(
                    f"Multiple packages for {split}; use a distinct comparison_id for each evaluation"
                )
            split_trials[split] = part
            release, _ = load_reference_release(
                identity.benchmark.version, split=split, source=release_source
            )
            split_rows[split] = (
                release.select_cases(include_slices=identity.benchmark.include_slices)
                if release.manifest.get("selection")
                else None
            )
        all_trials.extend(trials)
    split_metrics = {}
    for split, trials in split_trials.items():
        metrics = aggregate_trial_results(
            trials,
            n_trials_expected=len(trials),
            task_rows=split_rows[split],
            trials_per_task=identity.benchmark.n_trials,
        )
        if metrics.main_score is None:
            raise ValueError("Split summaries require release slice metadata")
        split_metrics[split] = metrics.model_dump(mode="json")
    totals = defaultdict(float)
    for trial in split_trials.get("test", []):
        if trial.dataset.get("slice", "core") in MAIN_SLICES:
            totals[trial.case_key] += score_for_primary(
                trial.metrics.get("rca_f1"), score_status=trial.score_status
            )
    combined = aggregate_trial_results(
        all_trials,
        n_trials_expected=len(all_trials),
        task_rows=(
            [row for rows in split_rows.values() for row in rows]
            if all(rows is not None for rows in split_rows.values())
            else None
        ),
        trials_per_task=identity.benchmark.n_trials,
    ).model_dump(mode="json")
    return {
        "main_scores": {
            "dev": split_metrics.get("dev", {}).get("main_score"),
            "test": split_metrics.get("test", {}).get("main_score"),
            "dev_test": combined["main_score"] if len(split_metrics) == 2 else None,
        },
        "split_metrics": split_metrics,
        "combined_metrics": combined if len(split_metrics) == 2 else None,
        "test_task_scores": {
            k: v / identity.benchmark.n_trials for k, v in totals.items()
        }
        or None,
    }
