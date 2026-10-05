"""Discover and summarize sessions for the session viewer."""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from nika.config import resolve_results_root
from nika.inspect.adapters import iter_jsonl
from nika.inspect.models import (
    Annotations,
    ArtifactFlags,
    BenchmarkRunSummary,
    ScoresResponse,
    SessionDetail,
    SessionFacets,
    SessionSummary,
)
from nika.utils.session_artifacts import (
    RUN_FILENAME,
    is_job_run_dir,
    iter_session_dirs,
    normalize_session_status,
    write_json_atomic,
)

ANNOTATIONS_FILENAME = "annotations.json"

ARTIFACT_FILES = {
    "run": RUN_FILENAME,
    "messages": "messages.jsonl",
    "nika": "nika.jsonl",
    "ground_truth": "ground_truth.json",
    "submission": "submission.json",
    "eval_metrics": "eval_metrics.json",
    "llm_judge": "llm_judge.json",
    "annotations": ANNOTATIONS_FILENAME,
}

# Trajectory search only reads the logs the timeline already shows; the answer
# key and score files are never scanned.
_SEARCHABLE_LOGS = (("agent", "messages.jsonl"), ("nika", "nika.jsonl"))

RAW_ALLOWLIST = frozenset(ARTIFACT_FILES.values())

# Answer key and scores stay hidden until the session stops running, so an
# agent that can reach the viewer cannot read them mid-run.
RUNNING_HIDDEN_ARTIFACTS = frozenset(
    {"ground_truth.json", "eval_metrics.json", "llm_judge.json"}
)

# Summaries are rebuilt only when a session artifact changes; the viewer
# polls the session list every few seconds.
_SUMMARY_CACHE: dict[tuple[str, str], tuple[tuple, "SessionSummary"]] = {}
_PARENT_JOB_CACHE: dict[str, tuple[tuple, dict[str, Any] | None]] = {}
_CACHE_MAX_ENTRIES = 50_000

_JOB_FILENAMES = (RUN_FILENAME, "benchmark_job.json", "RELEASE.lock.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class AmbiguousSessionError(ValueError):
    """A bare session id matches more than one session directory."""


def _artifact_flags(session_dir: Path) -> ArtifactFlags:
    return ArtifactFlags(
        **{key: (session_dir / name).is_file() for key, name in ARTIFACT_FILES.items()}
    )


def _metric(metrics: dict[str, Any] | None, key: str) -> Any:
    if not metrics:
        return None
    value = metrics.get(key)
    return value if value is not None else None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _stat_signature(paths: list[Path]) -> tuple:
    """Cheap change token: ``(mtime_ns, size)`` per path, ``None`` if missing."""
    signature: list[tuple[int, int] | None] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            signature.append(None)
            continue
        signature.append((stat.st_mtime_ns, stat.st_size))
    return tuple(signature)


def _cache_put(cache: dict, key: Any, value: Any) -> None:
    if len(cache) >= _CACHE_MAX_ENTRIES:
        cache.clear()
    cache[key] = value


def _load_parent_job(session_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    """If this session lives under ``…/<run>/trials/<id>``, load run job metadata.

    Memoized per run root: every trial of a run shares the same job file.
    """
    resolved = session_dir.resolve()
    parent = resolved.parent
    if parent.name != "trials":
        return None, None
    run_root = parent.parent
    signature = _stat_signature([run_root / name for name in _JOB_FILENAMES])
    cached = _PARENT_JOB_CACHE.get(str(run_root))
    if cached is not None and cached[0] == signature:
        return run_root, cached[1]
    job: dict[str, Any] | None = None
    for name in _JOB_FILENAMES:
        job = _read_json(run_root / name)
        if job is not None:
            break
    _cache_put(_PARENT_JOB_CACHE, str(run_root), (signature, job))
    return run_root, job


def _aware_iso(value: Any) -> str | None:
    """ISO timestamp with an explicit offset for the browser.

    ``start_time`` / ``end_time`` are written naive in server-local time; the
    browser would read them in its own time zone, so attach the server's.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return parsed.astimezone().isoformat()


def _start_time(run: dict[str, Any]) -> str | None:
    """``start_time`` is set at injection; sessions that fail during deploy only have ``created_at``."""
    return _aware_iso(run.get("start_time") or run.get("created_at"))


# nika.jsonl milestones of env → inject → agent → close → eval, in order.
_STAGE_MILESTONES = (
    ("env_start", "inject"),
    ("agent_start", "agent"),
    ("agent_end", "teardown"),
    ("agent_error", "teardown"),
    ("env_stop", "teardown"),
    ("session_cleared", "eval"),
)
_STAGE_RANK = {"deploy": 0, "inject": 1, "agent": 2, "teardown": 3, "eval": 4}
# messages.jsonl rows written before the agent process emits anything.
_AGENT_BOOT_EVENTS = frozenset(
    {"agent_start", "mcp_config", "prompt", "subprocess_start"}
)
_MESSAGES_TAIL_BYTES = 64 * 1024


def _last_agent_entry(path: Path) -> dict[str, Any] | None:
    """Last complete JSON row of ``messages.jsonl`` (tail read; logs grow large)."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _MESSAGES_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            return row
    return None


def _running_stage(session_dir: Path) -> str:
    stage = "deploy"
    milestones = dict(_STAGE_MILESTONES)
    for entry in iter_jsonl(session_dir / "nika.jsonl"):
        reached = milestones.get(str(entry.get("event")))
        if reached and _STAGE_RANK[reached] > _STAGE_RANK[stage]:
            stage = reached
    if stage != "agent":
        return stage
    last = _last_agent_entry(session_dir / "messages.jsonl")
    if last is None:
        return "agent: starting"
    phase = last.get("phase")
    label = f"agent: {phase}" if isinstance(phase, str) and phase else "agent"
    if last.get("event") in _AGENT_BOOT_EVENTS:
        return f"{label} (starting)"
    return label


def _inject_params(run: dict[str, Any]) -> dict[str, str]:
    """Extract human-facing inject/location params from the run fingerprint."""
    fp: Any = run.get("benchmark_fingerprint")
    if isinstance(fp, str):
        try:
            fp = json.loads(fp)
        except json.JSONDecodeError:
            fp = None
    if not isinstance(fp, dict):
        return {}
    inject = fp.get("inject")
    if not isinstance(inject, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in inject.items():
        if value is None or value == "":
            continue
        out[str(key)] = str(value)
    return out


def _benchmark_fields(session_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    run_root, job = _load_parent_job(session_dir)

    def pick(*values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None

    job = job or {}
    job_run_id = pick(job.get("run_id"), job.get("job_id"))
    benchmark_id = pick(run.get("benchmark_id"), job.get("benchmark_id"))
    benchmark_version = pick(run.get("benchmark_version"), job.get("version"))
    benchmark_split = pick(run.get("benchmark_split"), job.get("split"))
    benchmark_run_id = pick(
        run.get("benchmark_run_id"),
        run.get("benchmark_job_id"),
        job_run_id,
    )
    benchmark_official = pick(
        _as_bool(run.get("benchmark_official")),
        _as_bool(job.get("official")),
    )
    benchmark_n_trials = pick(
        _as_int(run.get("benchmark_n_trials")),
        _as_int(job.get("n_trials")),
    )
    scoring_id = pick(
        run.get("scoring_id"),
        (job.get("scoring") or {}).get("id")
        if isinstance(job.get("scoring"), dict)
        else None,
    )
    case_key = pick(run.get("case_key"))
    trial_id = pick(run.get("trial_id"), run.get("session_id"))
    trial_index = _as_int(run.get("trial_index"))

    is_benchmark = bool(
        benchmark_id
        or benchmark_run_id
        or run.get("case_key")
        or run.get("trial_id")
        or run_root is not None
    )

    if benchmark_id and benchmark_version:
        label = f"{benchmark_id}@{benchmark_version}"
    elif benchmark_id:
        label = str(benchmark_id)
    elif run_root is not None:
        label = run_root.name
    elif benchmark_run_id:
        label = str(benchmark_run_id)
    elif is_benchmark:
        label = "Benchmark"
    else:
        label = None

    return {
        "is_benchmark": is_benchmark,
        "case_key": str(case_key) if case_key else None,
        "trial_id": str(trial_id) if trial_id and is_benchmark else None,
        "trial_index": trial_index if is_benchmark else None,
        "benchmark_id": str(benchmark_id) if benchmark_id else None,
        "benchmark_version": str(benchmark_version) if benchmark_version else None,
        "benchmark_split": str(benchmark_split) if benchmark_split else None,
        "benchmark_run_id": str(benchmark_run_id) if benchmark_run_id else None,
        "benchmark_official": benchmark_official,
        "benchmark_n_trials": benchmark_n_trials,
        "scoring_id": str(scoring_id) if scoring_id else None,
        "benchmark_label": label,
    }


def _benchmark_run_key(summary: SessionSummary) -> str:
    if summary.benchmark_run_id:
        return f"run:{summary.benchmark_run_id}"
    if summary.benchmark_id:
        version = summary.benchmark_version or ""
        return f"id:{summary.benchmark_id}@{version}"
    if summary.benchmark_label:
        return f"label:{summary.benchmark_label}"
    return "benchmark"


def aggregate_benchmark_runs(
    sessions: list[SessionSummary],
) -> list[BenchmarkRunSummary]:
    groups: dict[str, list[SessionSummary]] = defaultdict(list)
    for session in sessions:
        if not session.is_benchmark:
            continue
        groups[_benchmark_run_key(session)].append(session)

    runs: list[BenchmarkRunSummary] = []
    for run_key, members in groups.items():
        head = members[0]
        finished = [s for s in members if s.status == "finished"]
        scores = [s.rca_f1 for s in members if s.rca_f1 is not None]
        runs.append(
            BenchmarkRunSummary(
                run_key=run_key,
                label=head.benchmark_label or "Benchmark",
                benchmark_id=head.benchmark_id,
                benchmark_version=head.benchmark_version,
                benchmark_split=head.benchmark_split,
                benchmark_run_id=head.benchmark_run_id,
                benchmark_official=head.benchmark_official,
                agent_type=head.agent_type,
                model=head.model,
                scoring_id=head.scoring_id,
                expected_trials=head.benchmark_n_trials,
                session_count=len(members),
                finished_count=len(finished),
                mean_rca_f1=(sum(scores) / len(scores)) if scores else None,
            )
        )

    runs.sort(key=lambda r: r.label.lower())
    return runs


def _session_key(session_dir: Path, results_root: Path | None = None) -> str:
    """Relative POSIX path under the results root; falls back to dir name.

    Uses ``absolute()`` (not ``resolve()``) so symlink components under the
    results root are preserved in the key (needed for folder-tree navigation).
    """
    if results_root is not None:
        try:
            return (
                session_dir.absolute()
                .relative_to(Path(results_root).absolute())
                .as_posix()
            )
        except ValueError:
            try:
                return (
                    session_dir.resolve()
                    .relative_to(Path(results_root).resolve())
                    .as_posix()
                )
            except ValueError:
                pass
    return session_dir.name


def summarize_session_dir(
    session_dir: Path, *, results_root: Path | None = None
) -> SessionSummary | None:
    key = (str(session_dir.absolute()), str(results_root or ""))
    watched = [session_dir / name for name in ARTIFACT_FILES.values()]
    trials_dir = session_dir.resolve().parent
    if trials_dir.name == "trials":
        watched.extend(trials_dir.parent / name for name in _JOB_FILENAMES)
    signature = _stat_signature(watched)
    cached = _SUMMARY_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    summary = _build_session_summary(session_dir, results_root=results_root)
    if summary is not None:
        _cache_put(_SUMMARY_CACHE, key, (signature, summary))
    return summary


def _build_session_summary(
    session_dir: Path, *, results_root: Path | None = None
) -> SessionSummary | None:
    run = _read_json(session_dir / RUN_FILENAME)
    if run is None:
        return None
    session_id = str(run.get("session_id") or session_dir.name)
    status = normalize_session_status(run)
    metrics = (
        None if status == "running" else _read_json(session_dir / "eval_metrics.json")
    )
    problem_names = run.get("problem_names") or []
    if not isinstance(problem_names, list):
        problem_names = []
    bench = _benchmark_fields(session_dir, run)

    return SessionSummary(
        session_id=session_id,
        session_key=_session_key(session_dir, results_root),
        session_dir=str(session_dir.resolve()),
        status=status,
        stage=_running_stage(session_dir) if status == "running" else None,
        lab_name=run.get("lab_name"),
        backend=run.get("backend"),
        scenario_name=run.get("scenario_name"),
        scenario_topo_size=run.get("scenario_topo_size"),
        agent_type=run.get("agent_type"),
        model=run.get("model"),
        llm_provider=run.get("llm_provider"),
        problem_names=[str(p) for p in problem_names if p],
        failure_domain=run.get("failure_domain"),
        inject_params=_inject_params(run),
        start_time=_start_time(run),
        end_time=_aware_iso(run.get("end_time")),
        outcome=run.get("outcome"),
        detection_score=_metric(metrics, "detection_score"),
        rca_f1=_metric(metrics, "rca_f1"),
        localization_f1=_metric(metrics, "localization_f1"),
        in_tokens=_metric(metrics, "in_tokens"),
        out_tokens=_metric(metrics, "out_tokens"),
        steps=_metric(metrics, "steps"),
        tool_calls=_metric(metrics, "tool_calls"),
        tags=load_annotations(session_dir).tags,
        artifacts=_artifact_flags(session_dir),
        **bench,
    )


def detail_session_dir(
    session_dir: Path, *, results_root: Path | None = None
) -> SessionDetail | None:
    summary = summarize_session_dir(session_dir, results_root=results_root)
    if summary is None:
        return None
    run = _read_json(session_dir / RUN_FILENAME) or {}
    return SessionDetail(
        **summary.model_dump(),
        run=run,
        task_description=run.get("task_description"),
    )


def find_session_dir(
    session_id: str, *, results_root: Path | None = None
) -> Path | None:
    # Session keys are built with ``absolute()`` so symlinked folders under
    # the results root stay in the key.  Match them lexically the same way:
    # resolving here would move a key under a symlinked child outside the
    # root and force a full scan.  Absolute ids and ``..`` are rejected, so a
    # lexical path cannot leave the root.
    root = Path(results_root or resolve_results_root()).absolute()
    raw = str(session_id or "").strip()
    if not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
        return None
    keyed = root / raw
    if (keyed / RUN_FILENAME).is_file() and not is_job_run_dir(keyed):
        return keyed
    matches: list[Path] = []
    for session_dir in iter_session_dirs(root):
        run = _read_json(session_dir / RUN_FILENAME) or {}
        sid = str(run.get("session_id") or session_dir.name)
        key = _session_key(session_dir, root)
        if session_id in {sid, session_dir.name, key}:
            matches.append(session_dir)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Ambiguous bare session_id — prefer a unique exact directory-name hit.
        named = [d for d in matches if d.name == session_id]
        if len(named) == 1:
            return named[0]
        keys = ", ".join(sorted(_session_key(d, root) for d in matches))
        raise AmbiguousSessionError(
            f"Session id {session_id!r} matches several sessions ({keys}); "
            "use the session key"
        )
    return None


def delete_session_result(session_dir: Path, *, results_root: Path) -> Path:
    """Delete one session result directory under ``results_root``.

    Refuses paths outside the results root and sessions still ``running``.
    Does not touch live labs under ``runtime/``.
    """
    root = Path(results_root).resolve()
    target = Path(session_dir).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Refusing to delete path outside results root: {target}"
        ) from exc
    if target == root:
        raise ValueError("Refusing to delete the results root itself")
    if not target.is_dir():
        raise FileNotFoundError(f"Session directory not found: {target}")
    if not (target / RUN_FILENAME).is_file():
        raise ValueError(f"Not a session directory (missing {RUN_FILENAME}): {target}")
    if is_job_run_dir(target):
        raise ValueError(f"Refusing to delete a benchmark job directory: {target}")
    run = _read_json(target / RUN_FILENAME) or {}
    status = normalize_session_status(run)
    if status == "running":
        raise ValueError(
            "Refusing to delete a running session result; stop the session first"
        )
    shutil.rmtree(target)
    return target


def list_selectable_roots(results_root: Path) -> list[dict[str, str]]:
    """List the results root and its immediate child folders that hold sessions."""
    base = Path(results_root).resolve()
    items: list[dict[str, str]] = [
        {
            "id": ".",
            "label": f"{base.name}/ (all)",
            "path": str(base),
        }
    ]
    if not base.is_dir():
        return items
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if (
            not child.is_dir()
            or child.name.startswith(".")
            or child.name == "0_summary"
        ):
            continue
        if "/" in child.name or "\\" in child.name or child.name in {".", ".."}:
            continue
        has_sessions = bool(iter_session_dirs(child)) or (child / "trials").is_dir()
        if not has_sessions and not (child / RUN_FILENAME).is_file():
            continue
        items.append(
            {
                "id": child.name,
                "label": child.name,
                # Keep the path under the results root (don't follow symlinks out).
                "path": str(child.absolute()),
            }
        )
    return items


def resolve_results_selection(
    results_root: Path, root_id: str | None, *, allow_outside: bool = True
) -> Path:
    """Resolve a UI-selected folder to a concrete results directory.

    Accepts:
    - ``.`` / empty — the inspect base root
    - an immediate child name under the base (symlink children allowed)
    - a relative path under the base (``..`` rejected)
    - an absolute path to an existing directory (local inspect may leave the
      base root so operators can point at another results tree without restart);
      ``allow_outside=False`` limits absolute paths to the base tree
    """
    base = Path(results_root).resolve()
    raw = str(root_id or "").strip()
    if not raw or raw in {".", ""}:
        return base

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if not allow_outside:
            try:
                resolved.relative_to(base)
            except ValueError as exc:
                raise ValueError(f"Results folder escapes root: {raw}") from exc
        if not resolved.is_dir():
            raise ValueError(f"Results folder not found: {raw}")
        return resolved

    if ".." in Path(raw).parts:
        raise ValueError(f"Invalid results folder: {raw}")
    # Relative path under the base (may contain `/` for nested folders).
    relative = base / raw
    try:
        relative.absolute().relative_to(base.absolute())
    except ValueError as exc:
        raise ValueError(f"Results folder escapes root: {raw}") from exc
    if not relative.is_dir():
        raise ValueError(f"Results folder not found: {raw}")
    return relative


def list_browse_entries(
    results_root: Path, *, path: str | None = None, allow_outside: bool = True
) -> dict[str, Any]:
    """List immediate child folders for the inspect path browser."""
    base = Path(results_root).resolve()
    active = resolve_results_selection(base, path, allow_outside=allow_outside)
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(active.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        children = []
    for child in children:
        try:
            if not child.is_dir():
                continue
            name = child.name
            if name.startswith(".") or name in {
                "0_summary",
                "node_modules",
                "__pycache__",
            }:
                continue
            if name in {".", ".."}:
                continue
            # Shallow only — avoid walking large trial trees just for a badge.
            has_sessions = (child / RUN_FILENAME).is_file() or (
                child / "trials"
            ).is_dir()
            if not has_sessions:
                for grand in child.iterdir():
                    if grand.is_dir() and (grand / RUN_FILENAME).is_file():
                        has_sessions = True
                        break
        except OSError:
            continue
        entries.append(
            {
                "name": name,
                "path": str(child.absolute()),
                "has_sessions": has_sessions,
            }
        )
    parent: str | None = None
    try:
        parent_path = active.parent
        if parent_path != active and (allow_outside or active != base):
            parent = str(parent_path.absolute())
    except OSError:
        parent = None
    return {
        "path": str(active.absolute()),
        "parent": parent,
        "base_root": str(base),
        "entries": entries,
    }


def discover_sessions(*, results_root: Path | None = None) -> list[SessionSummary]:
    """Load every session summary under the results root."""
    root = Path(results_root or resolve_results_root())
    sessions: list[SessionSummary] = []
    for session_dir in iter_session_dirs(root):
        try:
            summary = summarize_session_dir(session_dir, results_root=root)
        except (OSError, TypeError, ValueError):
            # Skip a session mid-write rather than failing the whole catalog.
            continue
        if summary is not None:
            sessions.append(summary)
    sessions.sort(key=lambda s: s.start_time or s.session_id, reverse=True)
    return sessions


def build_session_facets(sessions: list[SessionSummary]) -> SessionFacets:
    """Collect sorted distinct values for homepage filter dropdowns."""

    def uniq(values: list[str]) -> list[str]:
        return sorted({v for v in values if v}, key=str.lower)

    statuses = sorted({s.status for s in sessions})
    scenarios = uniq([s.scenario_name or "" for s in sessions])
    agents = uniq([s.agent_type or "" for s in sessions])
    models = uniq([s.model or "" for s in sessions])
    problems = uniq([p for s in sessions for p in s.problem_names])
    failure_domains = uniq([s.failure_domain or "" for s in sessions])
    topo_sizes = uniq([s.scenario_topo_size or "" for s in sessions])
    trial_indices = sorted(
        {s.trial_index for s in sessions if s.trial_index is not None}
    )
    return SessionFacets(
        statuses=statuses,
        scenarios=scenarios,
        agents=agents,
        models=models,
        problems=problems,
        failure_domains=failure_domains,
        topo_sizes=topo_sizes,
        trial_indices=trial_indices,
        tags=uniq([t for s in sessions for t in s.tags]),
    )


def filter_sessions(
    sessions: list[SessionSummary],
    *,
    status: Literal["running", "finished", "aborted", "error", "all"] = "all",
    scenario: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    problem: str | None = None,
    failure_domain: str | None = None,
    topo_size: str | None = None,
    trial_index: int | None = None,
    q: str | None = None,
    has_score: bool | None = None,
    tag: str | None = None,
) -> list[SessionSummary]:
    """Filter session summaries for the homepage list."""
    tag_v = tag.strip() if tag else None
    scenario_v = scenario.strip() if scenario else None
    agent_v = agent.strip() if agent else None
    model_v = model.strip() if model else None
    problem_v = problem.strip() if problem else None
    domain_v = failure_domain.strip() if failure_domain else None
    topo_v = topo_size.strip() if topo_size else None
    query = q.lower().strip() if q else None

    out: list[SessionSummary] = []
    for summary in sessions:
        if status != "all" and summary.status != status:
            continue
        if scenario_v and summary.scenario_name != scenario_v:
            continue
        if agent_v and summary.agent_type != agent_v:
            continue
        if model_v and summary.model != model_v:
            continue
        if problem_v and problem_v not in summary.problem_names:
            continue
        if domain_v and summary.failure_domain != domain_v:
            continue
        if topo_v and summary.scenario_topo_size != topo_v:
            continue
        if trial_index is not None and summary.trial_index != trial_index:
            continue
        if has_score is True and summary.rca_f1 is None:
            continue
        if has_score is False and summary.rca_f1 is not None:
            continue
        if tag_v and tag_v not in summary.tags:
            continue
        if query:
            haystack = " ".join(
                [
                    summary.session_id,
                    summary.scenario_name or "",
                    summary.agent_type or "",
                    summary.model or "",
                    summary.failure_domain or "",
                    " ".join(summary.problem_names),
                    " ".join(f"{k} {v}" for k, v in summary.inject_params.items()),
                    summary.case_key or "",
                    summary.trial_id or "",
                    summary.benchmark_id or "",
                    summary.benchmark_version or "",
                    summary.benchmark_split or "",
                    summary.benchmark_run_id or "",
                    summary.benchmark_label or "",
                    summary.scoring_id or "",
                    " ".join(summary.tags),
                ]
            ).lower()
            if query not in haystack:
                continue
        out.append(summary)
    return out


def list_sessions(
    *,
    results_root: Path | None = None,
    status: Literal["running", "finished", "aborted", "error", "all"] = "all",
    scenario: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    problem: str | None = None,
    failure_domain: str | None = None,
    topo_size: str | None = None,
    trial_index: int | None = None,
    q: str | None = None,
    has_score: bool | None = None,
) -> list[SessionSummary]:
    return filter_sessions(
        discover_sessions(results_root=results_root),
        status=status,
        scenario=scenario,
        agent=agent,
        model=model,
        problem=problem,
        failure_domain=failure_domain,
        topo_size=topo_size,
        trial_index=trial_index,
        q=q,
        has_score=has_score,
    )


def is_session_running(session_dir: Path) -> bool:
    run = _read_json(session_dir / RUN_FILENAME) or {}
    return normalize_session_status(run) == "running"


def load_scores(session_dir: Path) -> ScoresResponse:
    run = _read_json(session_dir / RUN_FILENAME) or {}
    session_id = str(run.get("session_id") or session_dir.name)
    if normalize_session_status(run) == "running":
        return ScoresResponse(
            session_id=session_id,
            submission=_read_json(session_dir / "submission.json"),
        )
    return ScoresResponse(
        session_id=session_id,
        eval_metrics=_read_json(session_dir / "eval_metrics.json"),
        ground_truth=_read_json(session_dir / "ground_truth.json"),
        submission=_read_json(session_dir / "submission.json"),
        llm_judge=_read_json(session_dir / "llm_judge.json"),
    )


def _contains_text(value: Any, needle: str) -> bool:
    if isinstance(value, str):
        return needle in value.lower()
    if isinstance(value, dict):
        return any(_contains_text(v, needle) for v in value.values())
    if isinstance(value, list):
        return any(_contains_text(v, needle) for v in value)
    return False


def session_content_hits(session_dir: Path, needle: str) -> list[str]:
    """Timeline event ids whose full log record contains ``needle`` (case-insensitive).

    Ids follow the timeline adapters (``agent-{i}`` / ``nika-{i}``) and match
    the untruncated record, not the slimmed ``raw`` the timeline API returns.
    """
    query = needle.lower().strip()
    if not query:
        return []
    hits: list[str] = []
    for prefix, filename in _SEARCHABLE_LOGS:
        for index, entry in enumerate(iter_jsonl(session_dir / filename)):
            if _contains_text(entry, query):
                hits.append(f"{prefix}-{index}")
    return hits


def load_annotations(session_dir: Path) -> Annotations:
    data = _read_json(session_dir / ANNOTATIONS_FILENAME)
    if data is None:
        return Annotations()
    try:
        return Annotations.model_validate(data)
    except ValidationError:
        return Annotations()


def save_annotations(session_dir: Path, annotations: Annotations) -> Annotations:
    """Replace ``annotations.json``; refused while the session is running."""
    if is_session_running(session_dir):
        raise ValueError("Annotations are read-only while the session is running")
    write_json_atomic(
        session_dir / ANNOTATIONS_FILENAME,
        annotations.model_dump(),
        ensure_ascii=False,
    )
    return annotations


def read_raw_artifact(session_dir: Path, filename: str) -> Any:
    if filename not in RAW_ALLOWLIST:
        raise FileNotFoundError(f"Artifact not allowlisted: {filename}")
    path = session_dir / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    # Live writers may leave a partial UTF-8 sequence or JSON document.
    text = path.read_text(encoding="utf-8", errors="replace")
    if filename.endswith(".jsonl"):
        lines: list[Any] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                lines.append({"_raw": line})
        return lines
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}
