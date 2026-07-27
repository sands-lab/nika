"""Create a leaderboard submission package from a finished release run."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from nika.config import REPO_ROOT, resolve_results_root
from nika.workflows.benchmark.release import (
    ReleaseError,
    load_release,
    load_run_config,
)
from nika.workflows.benchmark.trials import (
    expand_trials,
    is_valid_trial,
    trial_dir,
)
from nika.workflows.leaderboard.aggregate import (
    aggregate_trial_results,
    extract_trial_metrics,
)
from nika.workflows.leaderboard.hashing import sha256_file
from nika.workflows.leaderboard.meta_input import (
    MetaInputError,
    load_submission_dir,
    slugify_name,
)
from nika.workflows.leaderboard.schema import (
    FILES_FILENAME,
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    PRIMARY_METRIC,
    README_FILENAME,
    RESULTS_DIRNAME,
    SCHEMA_VERSION,
    TRIAL_RESULT_FILENAME,
    TRIALS_DIRNAME,
    BenchmarkIdentity,
    FileInventory,
    PackageIdentity,
    RunIdentity,
    SubmissionMetadata,
    TrialResult,
)
from nika.workflows.leaderboard.secrets import scan_value_for_issues


class LeaderboardPackError(ValueError):
    """Invalid release-run inputs for packing a leaderboard submission."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _relative_result_hint(result_dir: Path) -> str:
    try:
        return result_dir.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return result_dir.name


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise LeaderboardPackError(f"Expected JSON object at {path}")
    return data


def _write_json(path: Path, payload: Any) -> None:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_yaml(path: Path, payload: Any) -> None:
    if hasattr(payload, "model_dump"):
        data = payload.model_dump(mode="json", exclude_none=False)
    else:
        data = payload
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _resolve_metadata(
    *,
    submission_dir: str | Path | None,
    metadata: SubmissionMetadata | dict[str, Any] | None,
) -> tuple[SubmissionMetadata, str | None, str | None]:
    """Return metadata plus optional source paths for metadata.yaml / README.md."""
    if submission_dir is not None:
        try:
            meta, meta_path, readme_path = load_submission_dir(submission_dir)
        except MetaInputError as exc:
            raise LeaderboardPackError(str(exc)) from exc
        return meta, str(meta_path), str(readme_path)

    if metadata is None:
        raise LeaderboardPackError(
            "submission metadata is required (pass --submission DIR, or metadata=...)"
        )
    if isinstance(metadata, SubmissionMetadata):
        return metadata, None, None
    if isinstance(metadata, dict):
        try:
            from nika.workflows.leaderboard.meta_input import parse_metadata_payload

            return parse_metadata_payload(metadata), None, None
        except MetaInputError as exc:
            raise LeaderboardPackError(str(exc)) from exc
    raise LeaderboardPackError("metadata must be SubmissionMetadata or a dict")


def _package_folder_name(metadata: SubmissionMetadata, *, when: datetime) -> str:
    slug = slugify_name(metadata.info.name)
    return f"{when.strftime('%Y%m%d')}_{slug}"


def _trial_result_from_dir(
    *,
    trial_id: str,
    case_key: str,
    trial_index: int,
    scenario: str,
    problem: str,
    session_dir: Path,
) -> TrialResult:
    run_meta = _read_json(session_dir / "run.json")
    outcome = str(run_meta.get("outcome") or "")
    if outcome not in {"success", "agent_failed"}:
        raise LeaderboardPackError(
            f"Trial {trial_id} has invalid outcome {outcome!r} under {session_dir}"
        )
    metrics_path = session_dir / "eval_metrics.json"
    metrics = (
        extract_trial_metrics(_read_json(metrics_path))
        if metrics_path.is_file()
        else {}
    )

    return TrialResult(
        trial_id=trial_id,
        case_key=case_key,
        trial_index=trial_index,
        scenario=scenario,
        problem=problem,
        outcome=outcome,  # type: ignore[arg-type]
        metrics=metrics,
    )


def pack_leaderboard_submission(
    result_dir: str | Path,
    *,
    submission_dir: str | Path | None = None,
    metadata: SubmissionMetadata | dict[str, Any] | None = None,
    readme_text: str | None = None,
    out_dir: str | Path | None = None,
) -> Path:
    """Build a SWE-bench-style leaderboard package from an official release run.

    Prefer ``submission_dir`` containing edited ``metadata.yaml`` + ``README.md``.
    Programmatic callers may pass ``metadata`` and optional ``readme_text``.

    Returns the package directory path.
    """
    results_root = resolve_results_root(result_dir)
    if not results_root.is_dir():
        raise LeaderboardPackError(f"result_dir does not exist: {results_root}")

    run_cfg = load_run_config(results_root)
    if run_cfg is None:
        raise LeaderboardPackError(
            f"No run.json / benchmark_job.json under {results_root}"
        )
    if run_cfg.get("official") is not True:
        raise LeaderboardPackError(
            "Only official release runs can be packed (run.official must be true)"
        )

    version = str(run_cfg.get("version") or "")
    split = str(run_cfg.get("split") or "")
    if not version or not split:
        raise LeaderboardPackError("run.json missing version/split")

    try:
        release = load_release(version, split=split, verify_digest=True)
    except ReleaseError as exc:
        raise LeaderboardPackError(str(exc)) from exc

    for field, expected in (
        ("benchmark_id", release.id),
        ("benchmark_digest", release.benchmark_digest),
        ("cases_sha256", release.cases_sha256),
        ("case_count", release.case_count),
        ("n_trials", release.n_trials),
    ):
        actual = run_cfg.get(field)
        if actual != expected:
            raise LeaderboardPackError(
                f"run.json {field}={actual!r} does not match release {expected!r}"
            )

    trials = expand_trials(release.cases, release.n_trials)
    trial_results: list[TrialResult] = []

    for trial in trials:
        session_dir = trial_dir(results_root, trial.case_key, trial.trial_index)
        if not session_dir.is_dir() or not is_valid_trial(session_dir):
            raise LeaderboardPackError(
                f"Incomplete or missing trial {trial.trial_id} under {session_dir}"
            )
        result = _trial_result_from_dir(
            trial_id=trial.trial_id,
            case_key=trial.case_key,
            trial_index=trial.trial_index,
            scenario=str(trial.row["scenario"]),
            problem=str(trial.row["problem"]),
            session_dir=session_dir,
        )
        trial_results.append(result)

    run_json_path = results_root / "run.json"
    if not run_json_path.is_file():
        legacy = results_root / "benchmark_job.json"
        if not legacy.is_file():
            raise LeaderboardPackError(f"missing run.json under {results_root}")
        run_json_path = legacy
    source_run_sha256 = sha256_file(run_json_path)

    meta_model, _meta_src, readme_src = _resolve_metadata(
        submission_dir=submission_dir,
        metadata=metadata,
    )

    if readme_src is not None:
        readme_body = Path(readme_src).read_text(encoding="utf-8")
    elif readme_text is not None:
        readme_body = readme_text
    else:
        raise LeaderboardPackError(
            f"{README_FILENAME} is required "
            "(pass --submission DIR containing README.md, or readme_text=...)"
        )

    scoring = release.scoring if isinstance(release.scoring, dict) else {}
    created = _utc_now()
    identity = PackageIdentity(
        schema_version=SCHEMA_VERSION,  # type: ignore[arg-type]
        created_at=created.isoformat(),
        benchmark=BenchmarkIdentity(
            id=release.id,
            version=release.version,
            ref=release.ref,
            digest=release.benchmark_digest,
            split=release.split,  # type: ignore[arg-type]
            cases_sha256=release.cases_sha256,
            case_count=release.case_count,
            n_trials=release.n_trials,
            scoring_id=str(scoring.get("id") or ""),
            leaderboard_primary=str(
                scoring.get("leaderboard_primary") or PRIMARY_METRIC
            ),
        ),
        run=RunIdentity(
            run_id=str(run_cfg.get("run_id") or run_cfg.get("job_id") or ""),
            official=True,
            agent_type=str(run_cfg.get("agent_type") or ""),
            model=run_cfg.get("model"),
            llm_provider=run_cfg.get("llm_provider"),
            max_steps=run_cfg.get("max_steps"),
            case_timeout_sec=int(
                run_cfg.get("case_timeout_sec") or release.case_timeout_sec
            ),
            nika_git_commit=run_cfg.get("nika_git_commit"),
            source_result_dir=_relative_result_hint(results_root),
        ),
    )
    if not identity.run.run_id or not identity.run.agent_type:
        raise LeaderboardPackError("run.json must include run_id and agent_type")

    safety = scan_value_for_issues(
        {
            "metadata": meta_model.model_dump(mode="json"),
            "identity": identity.model_dump(mode="json"),
            "readme": readme_body,
        },
        label="submission",
    )
    if safety:
        raise LeaderboardPackError("; ".join(safety))

    if out_dir is not None:
        package_root = Path(out_dir)
    else:
        package_root = results_root / _package_folder_name(meta_model, when=created)
    if not package_root.is_absolute():
        package_root = (REPO_ROOT / package_root).resolve()
    else:
        package_root = package_root.resolve()

    if package_root.exists():
        shutil.rmtree(package_root)

    results_out = package_root / RESULTS_DIRNAME
    trials_out = results_out / TRIALS_DIRNAME
    trials_out.mkdir(parents=True)

    metrics = aggregate_trial_results(trial_results, n_trials_expected=len(trials))

    metadata_path = package_root / METADATA_FILENAME
    _write_yaml(metadata_path, meta_model)
    readme_path = package_root / README_FILENAME
    readme_path.write_text(readme_body, encoding="utf-8")

    identity_path = results_out / IDENTITY_FILENAME
    _write_yaml(identity_path, identity)
    metrics_path = results_out / METRICS_FILENAME
    _write_json(metrics_path, metrics)

    package_hashes: dict[str, str] = {
        METADATA_FILENAME: sha256_file(metadata_path),
        README_FILENAME: sha256_file(readme_path),
        f"{RESULTS_DIRNAME}/{IDENTITY_FILENAME}": sha256_file(identity_path),
        f"{RESULTS_DIRNAME}/{METRICS_FILENAME}": sha256_file(metrics_path),
    }

    for result in trial_results:
        trial_path = trials_out / result.trial_id
        trial_path.mkdir(parents=True, exist_ok=True)
        result_path = trial_path / TRIAL_RESULT_FILENAME
        _write_json(result_path, result)
        rel = (
            f"{RESULTS_DIRNAME}/{TRIALS_DIRNAME}/"
            f"{result.trial_id}/{TRIAL_RESULT_FILENAME}"
        )
        package_hashes[rel] = sha256_file(result_path)

    inventory = FileInventory(
        source_run_sha256=source_run_sha256,
        package=package_hashes,
    )
    files_path = package_root / FILES_FILENAME
    # files.json hashes exclude itself (validated by recompute of listed package files).
    _write_json(files_path, inventory)

    return package_root
