"""Discover and summarize sessions for the session viewer."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from nika.config import resolve_results_root
from nika.utils.session_artifacts import (
    RUN_FILENAME,
    is_finished_session,
    iter_session_dirs,
)
from nika.inspect.models import (
    ArtifactFlags,
    BenchmarkRunSummary,
    ScoresResponse,
    SessionDetail,
    SessionFacets,
    SessionSummary,
)

ARTIFACT_FILES = {
    "run": RUN_FILENAME,
    "messages": "messages.jsonl",
    "nika": "nika.jsonl",
    "ground_truth": "ground_truth.json",
    "submission": "submission.json",
    "eval_metrics": "eval_metrics.json",
    "llm_judge": "llm_judge.json",
}

RAW_ALLOWLIST = frozenset(ARTIFACT_FILES.values())

_JOB_FILENAMES = (RUN_FILENAME, "benchmark_job.json", "RELEASE.lock.json")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


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


def _load_parent_job(session_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    """If this session lives under ``…/<run>/trials/<id>``, load run job metadata."""
    resolved = session_dir.resolve()
    parent = resolved.parent
    if parent.name != "trials":
        return None, None
    run_root = parent.parent
    for name in _JOB_FILENAMES:
        job = _read_json(run_root / name)
        if job is not None:
            return run_root, job
    return run_root, None


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
    run = _read_json(session_dir / RUN_FILENAME)
    if run is None:
        return None
    session_id = str(run.get("session_id") or session_dir.name)
    finished = is_finished_session(run)
    metrics = _read_json(session_dir / "eval_metrics.json")
    problem_names = run.get("problem_names") or []
    if not isinstance(problem_names, list):
        problem_names = []
    bench = _benchmark_fields(session_dir, run)

    return SessionSummary(
        session_id=session_id,
        session_key=_session_key(session_dir, results_root),
        session_dir=str(session_dir.resolve()),
        status="finished" if finished else "running",
        lab_name=run.get("lab_name"),
        scenario_name=run.get("scenario_name"),
        scenario_topo_size=run.get("scenario_topo_size"),
        agent_type=run.get("agent_type"),
        model=run.get("model"),
        llm_provider=run.get("llm_provider"),
        problem_names=[str(p) for p in problem_names if p],
        failure_domain=run.get("failure_domain"),
        inject_params=_inject_params(run),
        start_time=run.get("start_time"),
        end_time=run.get("end_time"),
        outcome=run.get("outcome"),
        detection_score=_metric(metrics, "detection_score"),
        rca_f1=_metric(metrics, "rca_f1"),
        localization_f1=_metric(metrics, "localization_f1"),
        in_tokens=_metric(metrics, "in_tokens"),
        out_tokens=_metric(metrics, "out_tokens"),
        steps=_metric(metrics, "steps"),
        tool_calls=_metric(metrics, "tool_calls"),
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
    root = Path(results_root or resolve_results_root()).resolve()
    # Prefer explicit relative path keys (may contain slashes). Reject
    # absolute paths and anything that resolves outside the results root.
    raw = str(session_id or "").strip()
    if not raw or Path(raw).is_absolute():
        return None
    keyed = (root / raw).resolve()
    try:
        keyed.relative_to(root)
    except ValueError:
        return None
    if (keyed / RUN_FILENAME).is_file():
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
        # Ambiguous bare session_id — prefer an exact directory-name hit.
        for session_dir in matches:
            if session_dir.name == session_id:
                return session_dir
        return matches[0]
    return None


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
        if not child.is_dir() or child.name.startswith(".") or child.name == "0_summary":
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
                "path": str(child.resolve()),
            }
        )
    return items


def resolve_results_selection(
    results_root: Path, root_id: str | None
) -> Path:
    """Resolve a UI-selected folder id to a path under ``results_root``."""
    base = Path(results_root).resolve()
    if not root_id or root_id in {".", ""}:
        return base
    if root_id in {".."} or "/" in root_id or "\\" in root_id or root_id.startswith("."):
        raise ValueError(f"Invalid results folder: {root_id}")
    candidate = (base / root_id).resolve()
    if not candidate.is_dir():
        raise ValueError(f"Results folder not found: {root_id}")
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"Results folder escapes root: {root_id}") from exc
    return candidate


def discover_sessions(*, results_root: Path | None = None) -> list[SessionSummary]:
    """Load every session summary under the results root."""
    root = Path(results_root or resolve_results_root())
    sessions: list[SessionSummary] = []
    for session_dir in iter_session_dirs(root):
        summary = summarize_session_dir(session_dir, results_root=root)
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
    trial_indices = sorted({s.trial_index for s in sessions if s.trial_index is not None})
    return SessionFacets(
        statuses=statuses,
        scenarios=scenarios,
        agents=agents,
        models=models,
        problems=problems,
        failure_domains=failure_domains,
        topo_sizes=topo_sizes,
        trial_indices=trial_indices,
    )


def filter_sessions(
    sessions: list[SessionSummary],
    *,
    status: Literal["running", "finished", "all"] = "all",
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
    """Filter session summaries for the homepage list."""
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
                ]
            ).lower()
            if query not in haystack:
                continue
        out.append(summary)
    return out


def list_sessions(
    *,
    results_root: Path | None = None,
    status: Literal["running", "finished", "all"] = "all",
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


def load_scores(session_dir: Path) -> ScoresResponse:
    run = _read_json(session_dir / RUN_FILENAME) or {}
    session_id = str(run.get("session_id") or session_dir.name)
    return ScoresResponse(
        session_id=session_id,
        eval_metrics=_read_json(session_dir / "eval_metrics.json"),
        ground_truth=_read_json(session_dir / "ground_truth.json"),
        submission=_read_json(session_dir / "submission.json"),
        llm_judge=_read_json(session_dir / "llm_judge.json"),
    )


def read_raw_artifact(session_dir: Path, filename: str) -> Any:
    if filename not in RAW_ALLOWLIST:
        raise FileNotFoundError(f"Artifact not allowlisted: {filename}")
    path = session_dir / filename
    if not path.is_file():
        raise FileNotFoundError(filename)
    if filename.endswith(".jsonl"):
        lines: list[Any] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                lines.append(json.loads(line))
            except json.JSONDecodeError:
                lines.append({"_raw": line})
        return lines
    data = json.loads(path.read_text(encoding="utf-8"))
    return data
