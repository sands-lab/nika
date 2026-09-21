"""Trial layout: one ``result_dir`` = one run with ``trials/{case}__tNN/``."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nika.evaluator.result_log import MESSAGES_FILENAME
from nika.evaluator.trace_parser import AgentTraceParser
from nika.workflows.benchmark.resume import (
    benchmark_row_identity,
    cleanup_benchmark_session,
)
from nika.workflows.eval.session import build_eval_metrics_payload

TRIALS_DIRNAME = "trials"
VALID_TRIAL_OUTCOMES = frozenset({"success", "agent_failed"})
REQUIRED_TRIAL_ARTIFACTS = (
    "run.json",
    "ground_truth.json",
    MESSAGES_FILENAME,
    "eval_metrics.json",
)

_SAFE_TOKEN_RE = re.compile(r"[^a-zA-Z0-9._-]+")

# Short keys for human-facing trial labels (plan / progress panels).
_INJECT_LABEL_KEYS = (
    "host_name",
    "host_name_2",
    "intf_name",
    "router_name",
    "src",
    "dst",
    "node",
    "vip",
)
_INJECT_KEY_SHORT = {
    "host_name": "host",
    "host_name_2": "host2",
    "intf_name": "intf",
    "router_name": "router",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_token(value: str) -> str:
    cleaned = _SAFE_TOKEN_RE.sub("_", value.strip())
    return cleaned.strip("._-") or "case"


def _inject_label_parts(inject: Any) -> list[str]:
    """Compact inject summary for CLI labels (preferred keys first)."""
    if not isinstance(inject, dict) or not inject:
        return []
    parts: list[str] = []
    seen: set[str] = set()

    def _add(key: str, value: Any) -> None:
        if key in seen or value in (None, ""):
            return
        if isinstance(value, dict):
            return
        seen.add(key)
        short = _INJECT_KEY_SHORT.get(key, key)
        parts.append(f"{short}={value}")

    for key in _INJECT_LABEL_KEYS:
        if key in inject:
            _add(key, inject[key])
    for key, value in sorted(inject.items()):
        _add(str(key), value)
    return parts


def format_trial_label(row: dict[str, Any], *, trial_index: int) -> str:
    """Human-facing trial id: scenario/problem, size, inject, tNN."""
    scenario = str(row.get("scenario") or "?")
    problem = str(row.get("problem") or "?")
    bits = [f"{scenario}/{problem}"]
    topo = row.get("topo_size") or row.get("topo") or ""
    if topo != "":
        bits.append(str(topo))
    bits.extend(_inject_label_parts(row.get("inject")))
    bits.append(f"t{trial_index:02d}")
    return " ".join(bits)


def _inject_case_key_parts(inject: Any) -> list[str]:
    if not isinstance(inject, dict):
        return []
    parts: list[str] = []
    for key, value in sorted(inject.items()):
        if isinstance(value, dict):
            for nested_key, nested_value in sorted(value.items()):
                parts.append(sanitize_token(f"{key}-{nested_key}-{nested_value}"))
        else:
            parts.append(sanitize_token(f"{key}-{value}"))
    return parts


def case_key_for_row(row: dict[str, Any]) -> str:
    """Stable filesystem-safe case id from scenario / problem / deploy params."""
    identity = benchmark_row_identity(row)
    parts = [
        sanitize_token(str(identity["scenario"])),
        sanitize_token(str(identity["problem"])),
    ]
    for key in ("topo_size", "topo", "igp", "bgp_mode", "backend", "device_profile"):
        value = identity.get(key) or ""
        if value != "":
            parts.append(sanitize_token(str(value)))
    if identity.get("rpki"):
        parts.append("rpki")
    base = "__".join(parts)
    inject_parts = _inject_case_key_parts(identity.get("inject"))
    if not inject_parts:
        return base
    full = "__".join([base, *inject_parts])
    # Linux NAME_MAX is 255; trial dirname appends ``__tNN``.
    if len(full) + 5 <= 240:
        return full
    digest = hashlib.sha1(full.encode("utf-8")).hexdigest()[:16]
    return f"{base}__inj-{digest}"


def trial_dirname(case_key: str, trial_index: int) -> str:
    if trial_index < 1:
        raise ValueError("trial_index must be >= 1")
    return f"{case_key}__t{trial_index:02d}"


_TRIAL_SUFFIX_RE = re.compile(r"^(.*)__t(\d+)$")
_CATALOG_DEPLOY_KEYS = (
    "topo_size",
    "topo",
    "igp",
    "bgp_mode",
    "backend",
    "device_profile",
)


def task_id_for_row(row: dict[str, Any]) -> str:
    """Public case id. Same string as ``case_key_for_row``."""
    return case_key_for_row(row)


def parse_task_selector(selector: str) -> tuple[str, int | None]:
    """Split a public id into ``(task_id, trial_index)``.

    ``trial_index`` is set when *selector* is a trial dirname (``{task_id}__tNN``).
    """
    text = (selector or "").strip()
    if not text:
        raise ValueError("Task id must be a non-empty string.")
    match = _TRIAL_SUFFIX_RE.fullmatch(text)
    if match is None:
        return text, None
    task_id = match.group(1)
    trial_index = int(match.group(2))
    if not task_id or trial_index < 1:
        raise ValueError(f"Invalid trial id {selector!r}")
    return task_id, trial_index


def index_rows_by_task_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map each row's public ``task_id`` to the row. Duplicate ids raise."""
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        task_id = task_id_for_row(row)
        if task_id in indexed:
            raise ValueError(f"Duplicate task_id {task_id!r}")
        indexed[task_id] = row
    return indexed


def task_catalog_entry(row: dict[str, Any]) -> dict[str, Any]:
    """Compact operator-facing fields for ``nika benchmark list``."""
    entry: dict[str, Any] = {
        "task_id": task_id_for_row(row),
        "scenario": row["scenario"],
        "problem": row["problem"],
    }
    for key in _CATALOG_DEPLOY_KEYS:
        value = row.get(key)
        if value not in (None, ""):
            entry[key] = value
    if row.get("rpki"):
        entry["rpki"] = True
    return entry


def catalog_entries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index_rows_by_task_id(rows)
    return [task_catalog_entry(row) for row in rows]


def describe_task_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Decomposed case fields for ``nika benchmark describe``."""
    payload = task_catalog_entry(row)
    payload["inject"] = dict(row.get("inject") or {})
    problems = row.get("problems")
    if isinstance(problems, list) and len(problems) > 1:
        payload["problems"] = list(problems)
    if "root_causes" in row:
        payload["root_causes"] = row["root_causes"]
    status = row.get("root_causes_status")
    if status:
        payload["root_causes_status"] = status
    return payload


def resolve_catalog_row(rows: list[dict[str, Any]], selector: str) -> dict[str, Any]:
    """Find one row by ``task_id`` or trial dirname. Unknown ids raise."""
    task_id, _trial_index = parse_task_selector(selector)
    indexed = index_rows_by_task_id(rows)
    row = indexed.get(task_id)
    if row is None:
        raise ValueError(f"Unknown task id {selector!r}")
    return row


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
        return format_trial_label(self.row, trial_index=self.trial_index)


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


def select_trials(trials: list[Trial], selectors: list[str]) -> list[Trial]:
    """Filter expanded trials by ``task_id`` and/or ``{task_id}__tNN`` selectors."""
    if not selectors:
        return list(trials)
    by_task: dict[str, list[Trial]] = {}
    by_trial: dict[str, Trial] = {}
    for trial in trials:
        by_task.setdefault(trial.case_key, []).append(trial)
        by_trial[trial.trial_id] = trial
    selected: list[Trial] = []
    seen: set[str] = set()
    for raw in selectors:
        task_id, trial_index = parse_task_selector(raw)
        if trial_index is not None:
            trial = by_trial.get(trial_dirname(task_id, trial_index))
            if trial is None:
                raise ValueError(f"Unknown task id {raw!r}")
            if trial.trial_id not in seen:
                selected.append(trial)
                seen.add(trial.trial_id)
            continue
        matches = by_task.get(task_id)
        if not matches:
            raise ValueError(f"Unknown task id {raw!r}")
        for trial in matches:
            if trial.trial_id not in seen:
                selected.append(trial)
                seen.add(trial.trial_id)
    return selected



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


def _restore_success_eval_metrics(path: Path) -> bool:
    """Rebuild metrics for a solved trial interrupted before evaluation finished."""
    metrics_path = path / "eval_metrics.json"
    if metrics_path.is_file():
        return True

    gt = _read_json(path / "ground_truth.json")
    submission = _read_json(path / "submission.json")
    trajectory_path = path / MESSAGES_FILENAME
    if gt is None or submission is None or not trajectory_path.is_file():
        return False

    try:
        trace_metrics = AgentTraceParser(trace_path=str(trajectory_path)).parse_trace()
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        trace_metrics = {}
    payload = build_eval_metrics_payload(
        gt=gt,
        submission=submission,
        trace_metrics=trace_metrics,
    )
    try:
        metrics_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


def heal_trial_outcome(session_dir: str | Path, *, verbose: bool = False) -> bool:
    """Repair solved trials missing final metrics or ``outcome``.

    When ``status=finished``, rebuild missing metrics from a submission and
    infer an outcome that a kill/timeout prevented from being persisted:

    - ``success`` if ``submission.json`` is present
    - ``agent_failed`` otherwise

    Returns True when the directory is a valid counted trial afterwards.
    """
    from nika.workflows.benchmark.display import vprint

    path = Path(session_dir)
    if is_valid_trial(path):
        return True

    run_meta = _read_json(path / "run.json")
    if run_meta is None or run_meta.get("status") != "finished":
        return False

    outcome = run_meta.get("outcome")
    if outcome == "success" and _restore_success_eval_metrics(path):
        return is_valid_trial(path)
    if outcome in VALID_TRIAL_OUTCOMES:
        return trial_has_required_artifacts(path, outcome=str(outcome))

    # Submission proves the agent completed its work. Recover metrics when a
    # kill happened after submission but before evaluation/outcome persisted.
    if (path / "submission.json").is_file() and not _restore_success_eval_metrics(path):
        return False

    for name in REQUIRED_TRIAL_ARTIFACTS:
        if not (path / name).is_file():
            return False

    inferred = "success" if (path / "submission.json").is_file() else "agent_failed"
    run_meta["outcome"] = inferred
    run_meta["status"] = "finished"
    try:
        (path / "run.json").write_text(
            json.dumps(run_meta, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError:
        return False

    vprint(verbose, f"Healed trial outcome={inferred} under {path}")
    return is_valid_trial(path)


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
        if path.is_dir() and (is_valid_trial(path) or heal_trial_outcome(path)):
            completed += 1
    return completed


def scan_trials(
    *,
    trials: list[Trial],
    result_dir: str | Path,
    resume: bool,
    verbose: bool = False,
    announce: bool = True,
    mutate: bool = True,
) -> tuple[Path, list[int]]:
    """Return indices of trials that still need to run under ``result_dir``.

    When ``announce`` is False, skip resume/clear summary lines (used for
    internal re-scans during a run so the CLI is not spammed).

    When ``mutate`` is False, only classify slots (for preflight plan /
    confirmation) — do not clear ``--no-resume`` dirs or clean incomplete
    resume slots.
    """
    from nika.workflows.benchmark.display import vprint

    results_root = Path(result_dir)
    results_root.mkdir(parents=True, exist_ok=True)
    trials_dir = trials_root(results_root)
    trials_dir.mkdir(parents=True, exist_ok=True)
    total = len(trials)

    if not resume:
        if not mutate:
            return results_root, list(range(total))
        # Explicit re-run: wipe existing slots so init_session cannot leave a
        # hybrid running run.json over old artifacts that resume would delete.
        cleared = 0
        for trial in trials:
            path = trial_dir(results_root, trial.case_key, trial.trial_index)
            if path.exists():
                run_meta = _read_json(path / "run.json") or {}
                vprint(
                    verbose,
                    f"{trial.label} {trial.trial_id} clearing slot (--no-resume)",
                )
                cleanup_benchmark_session(
                    str(run_meta.get("session_id") or path.name),
                    path,
                )
                cleared += 1
        if cleared and announce and not verbose:
            print(f"Cleared {cleared} existing trial slot(s) (--no-resume)")
        return results_root, list(range(total))

    pending: list[int] = []
    completed = 0
    cleaned = 0

    for index, trial in enumerate(trials):
        path = trial_dir(results_root, trial.case_key, trial.trial_index)
        label = f"{trial.label} {trial.trial_id}"

        if path.is_dir() and is_valid_trial(path):
            completed += 1
            vprint(verbose, f"{label} skip (already complete: {path})")
            continue

        if mutate and path.is_dir() and heal_trial_outcome(path, verbose=verbose):
            completed += 1
            vprint(verbose, f"{label} skip (already complete: {path})")
            continue

        if path.exists():
            if not mutate:
                # Preflight only: leave incomplete slots untouched.
                pending.append(index)
                continue
            run_meta = _read_json(path / "run.json") or {}
            # Never delete a counted agent_failed / success trial.
            if is_valid_trial(path) or heal_trial_outcome(path, verbose=verbose):
                completed += 1
                vprint(verbose, f"{label} skip (already complete: {path})")
                continue
            vprint(verbose, f"{label} cleaning incomplete trial")
            cleanup_benchmark_session(
                str(run_meta.get("session_id") or path.name),
                path,
            )
            cleaned += 1

        pending.append(index)

    if announce:
        if completed and pending:
            print(
                f"Resuming run: {completed}/{total} trials complete, "
                f"{len(pending)} remaining under {results_root}"
            )
        elif not pending:
            print(f"All {total} trial(s) already complete under {results_root}")
    if cleaned and not verbose:
        print(f"Cleaned {cleaned} incomplete trial slot(s)")

    return results_root, pending


_RUN_IDENTITY_FIELDS = (
    "benchmark_id",
    "version",
    "split",
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
