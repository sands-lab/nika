"""Validate Hugging Face trajectory packages paired with scores submissions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from nika.config import REPO_ROOT
from nika.workflows.benchmark.release import ReleaseError, load_release
from nika.workflows.benchmark.trials import expand_trials
from nika.workflows.leaderboard.meta_input import MetaInputError, parse_metadata_payload
from nika.workflows.leaderboard.schema import (
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    README_FILENAME,
    RESULTS_DIRNAME,
    TRAJECTORIES_DIR_SUFFIX,
    TRAJECTORY_FORBIDDEN_NAME_FRAGMENTS,
    TRAJECTORY_OPTIONAL_SUCCESS_FILE,
    TRAJECTORY_REQUIRED_FILES,
    TRIAL_RESULT_FILENAME,
    TRIALS_DIRNAME,
    PackageIdentity,
    TrialResult,
)
from nika.workflows.leaderboard.secrets import scan_trajectory_package_dir
from nika.workflows.leaderboard.validate import ValidationReport


class TrajectoryValidateError(ValueError):
    """Trajectory package failed local validation."""


def _resolve_dir(path: str | Path) -> Path:
    root = Path(path)
    if not root.is_absolute():
        root = (REPO_ROOT / root).resolve()
    else:
        root = root.resolve()
    return root


def sibling_trajectories_dir(scores_dir: str | Path) -> Path:
    """Return the default sibling trajectories directory for a scores package."""
    root = _resolve_dir(scores_dir)
    return root.parent / f"{root.name}{TRAJECTORIES_DIR_SUFFIX}"


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _path_is_forbidden(rel: str) -> bool:
    lowered = rel.lower().replace("\\", "/")
    for fragment in TRAJECTORY_FORBIDDEN_NAME_FRAGMENTS:
        if fragment.lower() in lowered:
            return True
    return False


def validate_trajectory_package(
    trajectories_dir: str | Path,
    *,
    scores_dir: str | Path | None = None,
) -> ValidationReport:
    """Validate a trajectories package (optionally against its scores sibling)."""
    errors: list[str] = []
    warnings: list[str] = []
    root = _resolve_dir(trajectories_dir)

    if not root.is_dir():
        return ValidationReport(
            ok=False, errors=[f"trajectories dir not found: {root}"]
        )

    metadata_path = root / METADATA_FILENAME
    readme_path = root / README_FILENAME
    identity_path = root / IDENTITY_FILENAME
    trials_root = root / TRIALS_DIRNAME

    for required in (metadata_path, readme_path, identity_path, trials_root):
        if required == trials_root:
            if not trials_root.is_dir():
                errors.append(f"missing directory: {TRIALS_DIRNAME}/")
        elif not required.is_file():
            errors.append(f"missing file: {required.relative_to(root).as_posix()}")
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
            ok=False, errors=[f"invalid {IDENTITY_FILENAME}: {exc}"]
        )

    if not identity.scores_package:
        errors.append(f"{IDENTITY_FILENAME} missing scores_package")
    if not identity.trajectories_relpath:
        errors.append(f"{IDENTITY_FILENAME} missing trajectories_relpath")
    if not identity.run.official:
        errors.append("run.official must be true for trajectory submissions")

    expected_ids: set[str] | None = None
    try:
        release = load_release(
            identity.benchmark.version,
            split=identity.benchmark.split,
        )
    except ReleaseError as exc:
        errors.append(f"benchmark release load failed: {exc}")
        release = None

    if release is not None:
        expected_ids = {
            t.trial_id for t in expand_trials(release.cases, release.n_trials)
        }

    present_ids: set[str] = set()
    for trial_dir_path in sorted(p for p in trials_root.iterdir() if p.is_dir()):
        trial_id = trial_dir_path.name
        present_ids.add(trial_id)
        for name in TRAJECTORY_REQUIRED_FILES:
            path = trial_dir_path / name
            if not path.is_file():
                errors.append(
                    f"missing {trial_dir_path.relative_to(root).as_posix()}/{name}"
                )

        run_path = trial_dir_path / "run.json"
        outcome = ""
        if run_path.is_file():
            try:
                run_meta = _load_json(run_path)
                if isinstance(run_meta, dict):
                    outcome = str(run_meta.get("outcome") or "")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"invalid {trial_id}/run.json: {exc}")

        submission = trial_dir_path / TRAJECTORY_OPTIONAL_SUCCESS_FILE
        if outcome == "success" and not submission.is_file():
            errors.append(
                f"missing {trial_id}/{TRAJECTORY_OPTIONAL_SUCCESS_FILE} "
                "(required when outcome=success)"
            )

        for path in trial_dir_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if _path_is_forbidden(rel):
                errors.append(f"forbidden trajectory artifact: {rel}")

    if expected_ids is not None:
        missing = sorted(expected_ids - present_ids)
        extra = sorted(present_ids - expected_ids)
        if missing:
            errors.append(f"missing trials ({len(missing)}): {missing[:5]}")
        if extra:
            errors.append(f"unexpected trials ({len(extra)}): {extra[:5]}")

    if scores_dir is not None:
        scores_root = _resolve_dir(scores_dir)
        scores_identity_path = (
            scores_root / RESULTS_DIRNAME / IDENTITY_FILENAME
        )
        if not scores_identity_path.is_file():
            errors.append(
                f"scores package missing {RESULTS_DIRNAME}/{IDENTITY_FILENAME}"
            )
        else:
            try:
                scores_identity = PackageIdentity.model_validate(
                    _load_yaml(scores_identity_path)
                )
            except ValidationError as exc:
                errors.append(f"invalid scores identity: {exc}")
            else:
                if scores_identity.benchmark != identity.benchmark:
                    errors.append(
                        "trajectories benchmark identity does not match scores package"
                    )
                if scores_identity.run.run_id != identity.run.run_id:
                    errors.append(
                        "trajectories run.run_id does not match scores package"
                    )
                if identity.scores_package and identity.scores_package != scores_root.name:
                    errors.append(
                        f"scores_package {identity.scores_package!r} != "
                        f"scores dirname {scores_root.name!r}"
                    )
                if (
                    scores_identity.trajectories_relpath
                    and identity.trajectories_relpath
                    and scores_identity.trajectories_relpath
                    != identity.trajectories_relpath
                ):
                    errors.append(
                        "trajectories_relpath mismatch between scores and "
                        "trajectories packages"
                    )

                scores_trials = scores_root / RESULTS_DIRNAME / TRIALS_DIRNAME
                if scores_trials.is_dir():
                    score_ids: set[str] = set()
                    for trial_dir_path in scores_trials.iterdir():
                        if not trial_dir_path.is_dir():
                            continue
                        result_path = trial_dir_path / TRIAL_RESULT_FILENAME
                        if not result_path.is_file():
                            continue
                        try:
                            trial = TrialResult.model_validate(_load_json(result_path))
                        except ValidationError:
                            continue
                        score_ids.add(trial.trial_id)
                    if score_ids != present_ids:
                        errors.append(
                            "trial set differs between scores and trajectories packages"
                        )

    errors.extend(scan_trajectory_package_dir(root))
    return ValidationReport(ok=not errors, errors=errors, warnings=warnings)
