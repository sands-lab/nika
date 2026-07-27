"""Trial layout: one ``result_dir`` = one run with ``trials/{case}__tNN/``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nika.workflows.benchmark.resume import (
    benchmark_row_fingerprint,
    cleanup_benchmark_session,
)

TRIALS_DIRNAME = "trials"
VALID_TRIAL_OUTCOMES = frozenset({"success", "agent_failed"})
REQUIRED_TRIAL_ARTIFACTS = (
    "run.json",
    "ground_truth.json",
    "messages.jsonl",
    "eval_metrics.json",
)

_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_token(value: str) -> str:
    cleaned = _SAFE_TOKEN_RE.sub("_", value.strip())
    return cleaned.strip("._-") or "case"


def case_key_for_row(row: dict[str, Any]) -> str:
    """Stable filesystem-safe case id: ``{scenario}__{problem}__{fp8}``."""
    fp = benchmark_row_fingerprint(row)
    scenario = sanitize_token(str(row["scenario"]))
    problem = sanitize_token(str(row["problem"]))
    return f"{scenario}__{problem}__{fp[:8]}"


def trial_dirname(case_key: str, trial_index: int) -> str:
    if trial_index < 1:
        raise ValueError("trial_index must be >= 1")
    return f"{case_key}__t{trial_index:02d}"


def trials_root(result_dir: Path) -> Path:
    return result_dir / TRIALS_DIRNAME


def trial_dir(result_dir: Path, case_key: str, trial_index: int) -> Path:
    return trials_root(result_dir) / trial_dirname(case_key, trial_index)


@dataclass(frozen=True)
class Trial:
    """One deterministic (case, trial_index) execution unit."""

    case_index: int
    trial_index: int
    row: dict[str, Any]
    case_key: str
    trial_id: str

    @property
    def label(self) -> str:
        return (
            f"[{self.case_index + 1}:{self.trial_index}] "
            f"{self.row['scenario']}/{self.row['problem']}"
        )


def expand_trials(
    rows: list[dict[str, Any]],
    n_trials: int,
) -> list[Trial]:
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    trials: list[Trial] = []
    for case_index, row in enumerate(rows):
        case_key = case_key_for_row(row)
        for trial_index in range(1, n_trials + 1):
            trial_id = trial_dirname(case_key, trial_index)
            trials.append(
                Trial(
                    case_index=case_index,
                    trial_index=trial_index,
                    row=row,
                    case_key=case_key,
                    trial_id=trial_id,
                )
            )
    return trials


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def trial_has_required_artifacts(
    session_dir: Path,
    *,
    outcome: str,
) -> bool:
    for name in REQUIRED_TRIAL_ARTIFACTS:
        if not (session_dir / name).is_file():
            return False
    if outcome == "success" and not (session_dir / "submission.json").is_file():
        return False
    return True


def is_valid_trial(session_dir: str | Path) -> bool:
    """Return True when a trial directory is a counted completed trial."""
    path = Path(session_dir)
    run_meta = _read_json(path / "run.json")
    if run_meta is None:
        return False
    if run_meta.get("status") != "finished":
        return False
    outcome = run_meta.get("outcome")
    if outcome not in VALID_TRIAL_OUTCOMES:
        return False
    return trial_has_required_artifacts(path, outcome=str(outcome))


def count_completed_trials(
    *,
    trials: list[Trial],
    result_dir: str | Path,
) -> int:
    """Return how many trials under ``result_dir`` are already valid."""
    results_root = Path(result_dir)
    completed = 0
    for trial in trials:
        path = trial_dir(results_root, trial.case_key, trial.trial_index)
        if path.is_dir() and is_valid_trial(path):
            completed += 1
    return completed


def scan_trials(
    *,
    trials: list[Trial],
    result_dir: str | Path,
    resume: bool,
) -> tuple[Path, list[int]]:
    """Return indices of trials that still need to run under ``result_dir``."""
    results_root = Path(result_dir)
    results_root.mkdir(parents=True, exist_ok=True)
    trials_dir = trials_root(results_root)
    trials_dir.mkdir(parents=True, exist_ok=True)
    total = len(trials)

    if not resume:
        return results_root, list(range(total))

    pending: list[int] = []
    completed = 0

    for index, trial in enumerate(trials):
        path = trial_dir(results_root, trial.case_key, trial.trial_index)
        label = f"{trial.label} {trial.trial_id}"

        if path.is_dir() and is_valid_trial(path):
            completed += 1
            print(f"{label} skip (already complete: {path})")
            continue

        if path.exists():
            run_meta = _read_json(path / "run.json") or {}
            # Never delete a counted agent_failed / success trial.
            if is_valid_trial(path):
                completed += 1
                print(f"{label} skip (already complete: {path})")
                continue
            print(f"{label} cleaning incomplete trial")
            cleanup_benchmark_session(
                str(run_meta.get("session_id") or path.name),
                path,
            )

        pending.append(index)

    if completed and pending:
        print(
            f"Resuming run: {completed}/{total} trials complete, "
            f"{len(pending)} remaining under {results_root}"
        )
    elif not pending:
        print(f"All {total} trial(s) already complete under {results_root}")

    return results_root, pending


_RUN_IDENTITY_FIELDS = (
    "benchmark_id",
    "version",
    "benchmark_digest",
    "split",
    "cases_sha256",
    "agent_type",
    "model",
    "llm_provider",
    "max_steps",
    "n_trials",
    "case_timeout_sec",
    "official",
)


def run_config_identity(job: dict[str, Any]) -> dict[str, Any]:
    return {key: job.get(key) for key in _RUN_IDENTITY_FIELDS}


def assert_run_config_compatible(
    existing: dict[str, Any], proposed: dict[str, Any]
) -> None:
    """Refuse resume when run identity fields diverge."""
    old = run_config_identity(existing)
    new = run_config_identity(proposed)
    mismatches = [
        f"{key}: existing={old[key]!r} requested={new[key]!r}"
        for key in _RUN_IDENTITY_FIELDS
        if old.get(key) != new.get(key)
    ]
    if mismatches:
        raise ValueError(
            "Existing run under this --result_dir does not match the "
            "requested configuration:\n  - "
            + "\n  - ".join(mismatches)
            + "\nUse a different --result_dir or align agent/model/n_trials/release."
        )


def merge_run_config(
    *,
    existing: dict[str, Any] | None,
    proposed: dict[str, Any],
) -> dict[str, Any]:
    """Keep stable ``run_id`` / timestamps on resume; refresh updated_at."""
    if existing is None:
        now = _utc_now_iso()
        out = dict(proposed)
        out.setdefault("run_id", out.get("job_id") or out.get("run_id"))
        out.setdefault("job_id", out["run_id"])
        out["created_at"] = now
        out["updated_at"] = now
        return out

    assert_run_config_compatible(existing, proposed)
    out = dict(existing)
    out["updated_at"] = _utc_now_iso()
    # Allow refreshing git dirty/commit on resume without changing identity.
    for key in ("nika_git_commit", "nika_git_dirty"):
        if key in proposed:
            out[key] = proposed[key]
    return out
