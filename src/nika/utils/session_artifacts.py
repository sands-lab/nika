"""Session result directory helpers (no evaluator/agent imports)."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from nika.config import RESULTS_DIR

RUN_FILENAME = "run.json"
SESSION_EVENTS_FILENAME = "nika.jsonl"

SessionStatus = Literal["running", "finished", "aborted", "error"]

_TERMINAL_STATUSES = frozenset(
    {"finished", "aborted", "error", "interrupted", "failed"}
)

# Preferred key order for human-readable ``run.json``. Agent / problem fields
# come first; bulky inventory (``metadata.machine_identities``) and paths last.
# Unknown keys keep their relative order between the known blocks.
_RUN_JSON_KEY_ORDER: tuple[str, ...] = (
    "session_id",
    "agent_session_id",
    "status",
    "outcome",
    "agent_error",
    "trial_id",
    "trial_index",
    "case_key",
    "candidate_option_id",
    "scenario_name",
    "scenario_topo_size",
    "lab_name",
    "backend",
    "problem_names",
    "failure_domain",
    "agent_type",
    "llm_provider",
    "model",
    "reasoning_effort",
    "max_tokens",
    "task_description",
    "start_time",
    "end_time",
    "created_at",
    "updated_at",
    "eval_metrics",
    "benchmark_id",
    "benchmark_version",
    "benchmark_split",
    "benchmark_job_id",
    "benchmark_run_id",
    "benchmark_official",
    "benchmark_n_trials",
    "scoring_id",
    "nika_git_commit",
    "fault_ontology",
    "session_dir",
    "scenario_params",
    "topology_file",
    "runtime_workdir",
)


def order_run_json(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with preferred ``run.json`` key order.

    Keys listed in ``_RUN_JSON_KEY_ORDER`` are emitted first (when present).
    Remaining keys keep insertion order, except ``metadata`` which is always
    last so bulky machine inventory does not bury agent fields.
    """
    ordered: dict[str, Any] = {}
    for key in _RUN_JSON_KEY_ORDER:
        if key in payload:
            ordered[key] = payload[key]
    for key, value in payload.items():
        if key in ordered or key == "metadata":
            continue
        ordered[key] = value
    if "metadata" in payload:
        ordered["metadata"] = payload["metadata"]
    return ordered


def json_safe(value: Any) -> Any:
    """Convert ``value`` to JSON types; unknown objects become strings."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return str(value)


def write_json_atomic(
    path: str | Path,
    data: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = True,
    sort_keys: bool = False,
) -> None:
    """Write ``data`` as JSON via a same-directory temp file and ``os.replace``.

    Readers (resume scans, inspect, a parent watching a worker) never see a
    truncated or half-written document.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        # mkstemp uses 0600; keep the usual world-readable mode because
        # sandboxed agents may read mounted session files as another uid.
        try:
            mode = target.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), mode)
            json.dump(
                data,
                handle,
                indent=indent,
                ensure_ascii=ensure_ascii,
                sort_keys=sort_keys,
                default=str,
            )
            if indent is not None:
                handle.write("\n")
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_run_json(
    session_dir: str | Path, mutate: Callable[[dict[str, Any]], None]
) -> bool:
    """Read-modify-write ``run.json`` atomically; False when it is absent/invalid."""
    run_path = Path(session_dir) / RUN_FILENAME
    try:
        run_meta = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(run_meta, dict):
        return False
    mutate(run_meta)
    write_json_atomic(run_path, order_run_json(run_meta))
    return True


def is_finished_session(run_meta: dict) -> bool:
    """True when the session is no longer actively running.

    Includes normal finish as well as aborted/error terminals (any end_time
    or explicit non-running status). Callers that need only successful
    completion should check ``normalize_session_status(...) == "finished"``.
    """
    status = normalize_session_status(run_meta)
    if status != "running":
        return True
    return run_meta.get("end_time") is not None


def normalize_session_status(run_meta: dict) -> SessionStatus:
    """Map ``run.json`` status / end_time into the inspect status chip set."""
    raw = str(run_meta.get("status") or "").strip().lower()
    if raw in {"aborted", "interrupted"}:
        return "aborted"
    if raw in {"error", "failed"}:
        return "error"
    if raw == "finished":
        return "finished"
    if raw == "running" or not raw:
        if run_meta.get("end_time") is not None:
            return "finished"
        return "running"
    # Unknown explicit status with an end_time → treat as finished terminal.
    if run_meta.get("end_time") is not None or raw in _TERMINAL_STATUSES:
        return "finished"
    return "running"


def last_session_error(session_dir: str | Path) -> str | None:
    """Return the last ERROR message recorded in the session's ``nika.jsonl``.

    Lets a supervisor report *why* a session died instead of only that it did.
    Returns None when the log is absent or holds no ERROR record.
    """
    path = Path(session_dir) / SESSION_EVENTS_FILENAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("level") != "ERROR":
            continue
        message = str(record.get("message") or "").strip()
        if message:
            return message
    return None


def is_job_run_dir(path: str | Path) -> bool:
    """True when ``path`` is a benchmark job/result root, not a trial session.

    Job folders write ``run.json`` (release/job metadata) plus markers such as
    ``benchmark_job.json``, ``RELEASE.lock.json``, and/or a ``trials/`` tree.
    Those must not be indexed or closed as lab sessions — especially before any
    nested trial ``run.json`` exists (empty or partial ``trials/``).
    """
    root = Path(path)
    if (root / "trials").is_dir():
        return True
    if (root / "benchmark_job.json").is_file():
        return True
    if (root / "RELEASE.lock.json").is_file():
        return True
    return False


def iter_session_dirs(results_dir: str | Path | None = None) -> list[Path]:
    """Discover session/trial dirs that contain ``run.json``.

    Walks the results tree recursively and keeps only **session leaves**:
    directories with ``run.json`` that are not job/run containers (see
    :func:`is_job_run_dir`).

    Skips ``0_summary`` and hidden directories. Caps depth to avoid runaway walks.
    """
    root = Path(results_dir or RESULTS_DIR)
    if not root.is_dir():
        return []

    skip_names = {"0_summary", "node_modules", "__pycache__"}
    session_dirs: list[Path] = []

    def is_session_leaf(path: Path) -> bool:
        if not (path / RUN_FILENAME).is_file():
            return False
        if is_job_run_dir(path):
            return False
        return True

    def walk(dir_path: Path, depth: int) -> None:
        if depth > 16:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name in skip_names:
                continue
            if is_session_leaf(entry):
                session_dirs.append(entry)
                continue
            walk(entry, depth + 1)

    walk(root, 0)
    return session_dirs
