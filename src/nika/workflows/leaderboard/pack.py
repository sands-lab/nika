"""Create a leaderboard submission package from a finished release run."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
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
    build_rca_confusion,
)
from nika.workflows.leaderboard.hf_remote import remote_trajectories_relpath
from nika.workflows.leaderboard.meta_input import (
    MetaInputError,
    load_submission_dir,
    submission_slug,
)
from nika.workflows.leaderboard.schema import (
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    PRIMARY_METRIC,
    RCA_CONFUSION_FILENAME,
    README_FILENAME,
    RESULTS_DIRNAME,
    TRAJECTORIES_DIR_SUFFIX,
    TRAJECTORY_OPTIONAL_SUCCESS_FILE,
    TRAJECTORY_REQUIRED_FILES,
    TRIAL_RESULT_FILENAME,
    TRIALS_DIRNAME,
    BenchmarkIdentity,
    PackageIdentity,
    RunIdentity,
    SubmissionMetadata,
    TrialResult,
)
from nika.workflows.leaderboard.secrets import (
    HOST_PATH_PATTERN,
    PATH_START,
    scan_value_for_issues,
)
from nika.workflows.leaderboard.trial_results import (
    TrialResultError,
    trial_result_from_dir,
)


class LeaderboardPackError(ValueError):
    """Invalid release-run inputs for packing a leaderboard submission."""


@dataclass(frozen=True)
class PackResult:
    """Scores package plus sibling trajectories package from one pack run."""

    scores_dir: Path
    trajectories_dir: Path


def _host_path_redact(results_root: Path) -> re.Pattern[str]:
    """Match host-local paths: the validator's rule plus this host's roots."""
    roots = {
        str(path)
        for root in (REPO_ROOT, resolve_results_root(), results_root)
        for path in (root, root.resolve())
        if path.parent != path
    }
    # Roots are absolute, so each one starts with the "/" that PATH_START matches.
    local = "|".join(re.escape(r[1:]) for r in sorted(roots, key=len, reverse=True))
    checkout = rf"{PATH_START}(?:{local})(?![\w.-])[^\s\"'\\]*"
    return re.compile(rf"{checkout}|{HOST_PATH_PATTERN}")


# Agents can print private keys found on lab devices (e.g. a lab's TLS key).
# A JSONL record keeps the key's line breaks as literal ``\n`` escapes.
_PEM_KIND = r"(?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY"
_PEM_BLOCK_REDACT = re.compile(
    rf"-----BEGIN ({_PEM_KIND})-----.*?-----END \1-----", re.DOTALL
)
# A key cut off before its END line: drop the base64 that follows BEGIN.
_PEM_HEAD_REDACT = re.compile(
    rf"-----BEGIN {_PEM_KIND}-----(?:\\[nr]|\s|[A-Za-z0-9+/=])*"
)


def _redact_private_keys(text: str) -> str:
    text = _PEM_BLOCK_REDACT.sub("<REDACTED_PRIVATE_KEY>", text)
    return _PEM_HEAD_REDACT.sub("<REDACTED_PRIVATE_KEY>", text)


def _redact_text(text: str, path_re: re.Pattern[str]) -> str:
    return path_re.sub("<REDACTED_PATH>", _redact_private_keys(text))


def _sanitize_json_value(value: Any, path_re: re.Pattern[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, path_re)
    if isinstance(value, dict):
        return {k: _sanitize_json_value(v, path_re) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_value(v, path_re) for v in value]
    return value


def _write_sanitized_trajectory_file(
    src: Path,
    dest: Path,
    *,
    trial_id: str,
    path_re: re.Pattern[str],
) -> None:
    raw = src.read_text(encoding="utf-8")
    if src.name == "run.json":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise LeaderboardPackError(f"Expected JSON object at {src}")
        data = _sanitize_json_value(data, path_re)
        if isinstance(data, dict):
            data["session_dir"] = trial_id
        dest.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    if src.suffix == ".json":
        data = json.loads(raw)
        data = _sanitize_json_value(data, path_re)
        dest.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return
    if src.suffix == ".jsonl":
        with dest.open("w", encoding="utf-8") as output:
            # JSONL records end at "\n" only; splitlines() also breaks on
            # U+0085/U+2028 etc. that tool output can carry inside a string.
            lines = raw.split("\n")
            if lines and not lines[-1]:
                lines.pop()
            for line_number, line in enumerate(lines, start=1):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LeaderboardPackError(
                        f"{trial_id}/{src.name}:{line_number}: invalid JSONL: {exc}"
                    ) from exc
                output.write(
                    json.dumps(_sanitize_json_value(data, path_re), ensure_ascii=False)
                    + "\n"
                )
        return
    # Plain text
    dest.write_text(_redact_text(raw, path_re), encoding="utf-8")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _relative_result_hint(result_dir: Path) -> str:
    try:
        return result_dir.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return result_dir.name


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
) -> tuple[SubmissionMetadata, str | None]:
    """Return metadata plus an optional source path for README.md."""
    if submission_dir is not None:
        try:
            meta, _meta_path, readme_path = load_submission_dir(submission_dir)
        except MetaInputError as exc:
            raise LeaderboardPackError(str(exc)) from exc
        return meta, str(readme_path)

    if metadata is None:
        raise LeaderboardPackError(
            "submission metadata is required (pass --submission DIR, or metadata=...)"
        )
    if isinstance(metadata, SubmissionMetadata):
        return metadata, None
    if isinstance(metadata, dict):
        try:
            from nika.workflows.leaderboard.meta_input import parse_metadata_payload

            return parse_metadata_payload(metadata), None
        except MetaInputError as exc:
            raise LeaderboardPackError(str(exc)) from exc
    raise LeaderboardPackError("metadata must be SubmissionMetadata or a dict")


def _package_folder_name(metadata: SubmissionMetadata, *, when: datetime) -> str:
    slug = submission_slug(metadata)
    return f"{when.strftime('%Y%m%d')}_{slug}"


def _copy_trajectory_trial_files(
    *,
    session_dir: Path,
    dest_dir: Path,
    outcome: str,
    trial_id: str,
    path_re: re.Pattern[str],
) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in TRAJECTORY_REQUIRED_FILES:
        src = session_dir / name
        if not src.is_file():
            raise LeaderboardPackError(
                f"Trial under {session_dir} missing required trajectory file {name}"
            )
        _write_sanitized_trajectory_file(
            src, dest_dir / name, trial_id=trial_id, path_re=path_re
        )

    submission_src = session_dir / TRAJECTORY_OPTIONAL_SUCCESS_FILE
    if outcome == "success":
        if not submission_src.is_file():
            raise LeaderboardPackError(
                f"Trial under {session_dir} outcome=success but missing "
                f"{TRAJECTORY_OPTIONAL_SUCCESS_FILE}"
            )
        _write_sanitized_trajectory_file(
            submission_src,
            dest_dir / TRAJECTORY_OPTIONAL_SUCCESS_FILE,
            trial_id=trial_id,
            path_re=path_re,
        )
    elif submission_src.is_file():
        _write_sanitized_trajectory_file(
            submission_src,
            dest_dir / TRAJECTORY_OPTIONAL_SUCCESS_FILE,
            trial_id=trial_id,
            path_re=path_re,
        )


def pack_leaderboard_submission(
    result_dir: str | Path,
    *,
    submission_dir: str | Path | None = None,
    metadata: SubmissionMetadata | dict[str, Any] | None = None,
    readme_text: str | None = None,
    out_dir: str | Path | None = None,
) -> PackResult:
    """Build scores + trajectories packages from an official release run.

    Prefer ``submission_dir`` containing edited ``metadata.yaml`` + ``README.md``.
    Programmatic callers may pass ``metadata`` and optional ``readme_text``.

    Returns :class:`PackResult` with ``scores_dir`` and sibling
    ``trajectories_dir`` (``{scores}_trajectories/``).
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
    submission_mode = (run_cfg.get("submission") or {}).get("mode", "structured")
    if submission_mode != "structured":
        # Leaderboard scores assume the agent's own model submits its answer.
        raise LeaderboardPackError(
            f"Only structured-submission runs can be packed (got {submission_mode!r})"
        )

    version = str(run_cfg.get("version") or "")
    split = str(run_cfg.get("split") or "")
    if not version or not split:
        raise LeaderboardPackError("run.json missing version/split")

    try:
        release = load_release(version, split=split)
    except ReleaseError as exc:
        raise LeaderboardPackError(str(exc)) from exc

    for field, expected in (
        ("benchmark_id", release.id),
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
    session_by_trial_id: dict[str, Path] = {}

    for trial in trials:
        session_dir = trial_dir(results_root, trial.case_key, trial.trial_index)
        if not session_dir.is_dir() or not is_valid_trial(session_dir):
            raise LeaderboardPackError(
                f"Incomplete or missing trial {trial.trial_id} under {session_dir}"
            )
        try:
            result = trial_result_from_dir(
                trial_id=trial.trial_id,
                case_key=trial.case_key,
                trial_index=trial.trial_index,
                scenario=str(trial.row["scenario"]),
                problem=str(trial.row["problem"]),
                session_dir=session_dir,
            )
        except TrialResultError as exc:
            raise LeaderboardPackError(str(exc)) from exc
        trial_results.append(result)
        session_by_trial_id[trial.trial_id] = session_dir

    meta_model, readme_src = _resolve_metadata(
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
    package_name = (
        Path(out_dir).name
        if out_dir is not None
        else _package_folder_name(meta_model, when=created)
    )
    if out_dir is not None and not re.fullmatch(
        rf"\d{{8}}_{re.escape(submission_slug(meta_model))}",
        package_name,
    ):
        raise LeaderboardPackError(
            "--out directory name must be YYYYMMDD_<slug> matching agent.framework and agent.model"
        )
    try:
        traj_relpath = remote_trajectories_relpath(release.version, package_name)
    except ValueError as exc:
        raise LeaderboardPackError(str(exc)) from exc

    identity = PackageIdentity(
        created_at=created.isoformat(),
        benchmark=BenchmarkIdentity(
            id=release.id,
            version=release.version,
            ref=release.ref,
            split=release.split,  # type: ignore[arg-type]
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
            reasoning_effort=run_cfg.get("reasoning_effort"),
            case_timeout_sec=int(
                run_cfg.get("case_timeout_sec")
                if run_cfg.get("case_timeout_sec") is not None
                else 2400
            ),
            nika_git_commit=run_cfg.get("nika_git_commit"),
            source_result_dir=_relative_result_hint(results_root),
        ),
        trajectories_relpath=traj_relpath,
        scores_package=None,
    )
    if not identity.run.run_id or not identity.run.agent_type:
        raise LeaderboardPackError("run.json must include run_id and agent_type")

    traj_identity = PackageIdentity(
        created_at=identity.created_at,
        benchmark=identity.benchmark,
        run=identity.run,
        trajectories_relpath=traj_relpath,
        scores_package=package_name,
    )

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
        package_root = results_root / package_name
    if not package_root.is_absolute():
        package_root = (REPO_ROOT / package_root).resolve()
    else:
        package_root = package_root.resolve()

    traj_root = package_root.parent / f"{package_root.name}{TRAJECTORIES_DIR_SUFFIX}"

    for root in (package_root, traj_root):
        if root.exists():
            shutil.rmtree(root)

    results_out = package_root / RESULTS_DIRNAME
    trials_out = results_out / TRIALS_DIRNAME
    trials_out.mkdir(parents=True)

    metrics = aggregate_trial_results(trial_results, n_trials_expected=len(trials))
    confusion = build_rca_confusion(trial_results)

    metadata_path = package_root / METADATA_FILENAME
    _write_yaml(metadata_path, meta_model)
    readme_path = package_root / README_FILENAME
    readme_path.write_text(readme_body, encoding="utf-8")

    identity_path = results_out / IDENTITY_FILENAME
    _write_yaml(identity_path, identity)
    metrics_path = results_out / METRICS_FILENAME
    _write_json(metrics_path, metrics)
    confusion_path = results_out / RCA_CONFUSION_FILENAME
    _write_json(confusion_path, confusion)

    for result in trial_results:
        trial_path = trials_out / result.trial_id
        trial_path.mkdir(parents=True, exist_ok=True)
        result_path = trial_path / TRIAL_RESULT_FILENAME
        _write_json(result_path, result)

    # Sibling trajectories package (HF layout; no results/ wrapper).
    traj_trials = traj_root / TRIALS_DIRNAME
    traj_trials.mkdir(parents=True)
    _write_yaml(traj_root / METADATA_FILENAME, meta_model)
    (traj_root / README_FILENAME).write_text(readme_body, encoding="utf-8")
    _write_yaml(traj_root / IDENTITY_FILENAME, traj_identity)

    path_re = _host_path_redact(results_root)
    for result in trial_results:
        session_dir = session_by_trial_id[result.trial_id]
        _copy_trajectory_trial_files(
            session_dir=session_dir,
            dest_dir=traj_trials / result.trial_id,
            outcome=result.outcome,
            trial_id=result.trial_id,
            path_re=path_re,
        )

    traj_safety = scan_value_for_issues(
        {
            "metadata": meta_model.model_dump(mode="json"),
            "identity": traj_identity.model_dump(mode="json"),
            "readme": readme_body,
        },
        label="trajectories",
    )
    if traj_safety:
        raise LeaderboardPackError("; ".join(traj_safety))

    return PackResult(scores_dir=package_root, trajectories_dir=traj_root)
