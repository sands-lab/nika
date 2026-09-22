"""Pydantic models for the session inspect viewer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventSource = Literal["agent", "nika"]
EventKind = Literal[
    "lifecycle",
    "llm",
    "tool_call",
    "tool_result",
    "tool_error",
    "phase",
    "score",
    "submission",
    "system",
    "other",
]


class ToolPayload(BaseModel):
    name: str | None = None
    input: Any = None
    output: Any = None
    tool_call_id: str | None = None
    error: str | None = None


class CanonicalTraceEvent(BaseModel):
    """Normalized event for the merged agent + NIKA timeline."""

    id: str
    timestamp: str | None = None
    source: EventSource
    kind: EventKind
    title: str
    summary: str = ""
    phase: str | None = None
    event: str | None = None
    tool: ToolPayload | None = None
    duration_ms: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ArtifactFlags(BaseModel):
    run: bool = False
    messages: bool = False
    nika: bool = False
    ground_truth: bool = False
    submission: bool = False
    eval_metrics: bool = False
    llm_judge: bool = False


class SessionSummary(BaseModel):
    session_id: str
    # Stable unique id for API routing when the same trial id appears in
    # multiple benchmark runs (relative path under the results root).
    session_key: str | None = None
    session_dir: str
    status: Literal["running", "finished", "aborted", "error"]
    lab_name: str | None = None
    scenario_name: str | None = None
    scenario_topo_size: str | None = None
    agent_type: str | None = None
    model: str | None = None
    llm_provider: str | None = None
    problem_names: list[str] = Field(default_factory=list)
    failure_domain: str | None = None
    # Inject / localization params from the trial fingerprint (host_name, intf, …).
    inject_params: dict[str, str] = Field(default_factory=dict)
    start_time: str | None = None
    end_time: str | None = None
    outcome: str | None = None
    detection_score: float | None = None
    rca_f1: float | None = None
    localization_f1: float | None = None
    in_tokens: int | None = None
    out_tokens: int | None = None
    steps: int | None = None
    tool_calls: int | None = None
    artifacts: ArtifactFlags = Field(default_factory=ArtifactFlags)
    # Present when this session is a benchmark trial (or stamped from a job).
    is_benchmark: bool = False
    case_key: str | None = None
    trial_id: str | None = None
    trial_index: int | None = None
    benchmark_id: str | None = None
    benchmark_version: str | None = None
    benchmark_split: str | None = None
    benchmark_run_id: str | None = None
    benchmark_official: bool | None = None
    benchmark_n_trials: int | None = None
    scoring_id: str | None = None
    benchmark_label: str | None = None


class BenchmarkRunSummary(BaseModel):
    """Aggregated view of sessions that share one benchmark run."""

    run_key: str
    label: str
    benchmark_id: str | None = None
    benchmark_version: str | None = None
    benchmark_split: str | None = None
    benchmark_run_id: str | None = None
    benchmark_official: bool | None = None
    agent_type: str | None = None
    model: str | None = None
    scoring_id: str | None = None
    expected_trials: int | None = None
    session_count: int = 0
    finished_count: int = 0
    mean_rca_f1: float | None = None


class SessionDetail(SessionSummary):
    run: dict[str, Any] = Field(default_factory=dict)
    task_description: str | None = None


class SessionFacets(BaseModel):
    """Distinct filter values present in the results root."""

    statuses: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)
    agents: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    failure_domains: list[str] = Field(default_factory=list)
    topo_sizes: list[str] = Field(default_factory=list)
    trial_indices: list[int] = Field(default_factory=list)


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    benchmarks: list[BenchmarkRunSummary] = Field(default_factory=list)
    facets: SessionFacets = Field(default_factory=SessionFacets)
    results_root: str
    selected_root: str = "."
    total: int


class ResultsRootOption(BaseModel):
    id: str
    label: str
    path: str


class ResultsRootsResponse(BaseModel):
    base_root: str
    selected_root: str = "."
    results_root: str
    roots: list[ResultsRootOption] = Field(default_factory=list)


class BrowseEntry(BaseModel):
    name: str
    path: str
    has_sessions: bool = False


class BrowseResponse(BaseModel):
    path: str
    parent: str | None = None
    base_root: str
    entries: list[BrowseEntry] = Field(default_factory=list)


class TimelineResponse(BaseModel):
    session_id: str
    events: list[CanonicalTraceEvent]
    total: int


class ScoresResponse(BaseModel):
    session_id: str
    eval_metrics: dict[str, Any] | None = None
    ground_truth: dict[str, Any] | None = None
    submission: dict[str, Any] | None = None
    llm_judge: dict[str, Any] | None = None
