"""Versioned leaderboard submission schemas (schema_version ``1``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "1"

METADATA_FILENAME = "metadata.yaml"
README_FILENAME = "README.md"
FILES_FILENAME = "files.json"
RESULTS_DIRNAME = "results"
IDENTITY_FILENAME = "identity.yaml"
METRICS_FILENAME = "metrics.json"
TRIALS_DIRNAME = "trials"
TRIAL_RESULT_FILENAME = "result.json"

PRIMARY_METRIC = "rca_f1"

SCORE_METRIC_KEYS = (
    "detection_score",
    "localization_accuracy",
    "localization_precision",
    "localization_recall",
    "localization_f1",
    "rca_accuracy",
    "rca_precision",
    "rca_recall",
    "rca_f1",
)

TRACE_METRIC_KEYS = (
    "in_tokens",
    "out_tokens",
    "steps",
    "tool_calls",
    "tool_errors",
)


class SubmissionInfo(BaseModel):
    """User-facing submission identity (leaderboard display)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    authors: str = Field(..., min_length=1)
    org: str | None = None
    site: str | None = None
    report: str | None = None
    logo: str | None = None
    email: str | None = None
    github: str | None = None


class SubmissionAgent(BaseModel):
    """Required agent/system metadata for leaderboard submissions."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(..., min_length=1)
    framework: str = Field(..., min_length=1)
    tools: list[str]
    skills: list[str]
    optimization_methods: list[str]
    tags: list[str]
    os_model: bool = False
    os_system: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tools", "skills", "optimization_methods", "tags")
    @classmethod
    def _non_empty_strings(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("list entries must be non-empty strings")
        return value


class SubmissionMetadata(BaseModel):
    """Fixed ``metadata.yaml`` package root (user-filled)."""

    model_config = ConfigDict(extra="forbid")

    info: SubmissionInfo
    agent: SubmissionAgent


class BenchmarkIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    ref: str
    digest: str
    split: Literal["dev", "test"]
    cases_sha256: str
    case_count: int = Field(..., ge=1)
    n_trials: int = Field(..., ge=1)
    scoring_id: str
    leaderboard_primary: str = PRIMARY_METRIC


class RunIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    official: bool
    agent_type: str
    model: str | None = None
    llm_provider: str | None = None
    max_steps: int | None = None
    case_timeout_sec: int
    nika_git_commit: str | None = None
    source_result_dir: str | None = None


class PackageIdentity(BaseModel):
    """Machine-written ``results/identity.yaml`` (benchmark + run binding)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = SCHEMA_VERSION
    created_at: str
    benchmark: BenchmarkIdentity
    run: RunIdentity


class TrialResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trial_id: str
    case_key: str
    trial_index: int = Field(..., ge=1)
    scenario: str
    problem: str
    outcome: Literal["success", "agent_failed"]
    metrics: dict[str, float | int | None] = Field(default_factory=dict)


class AggregatedMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_metric: str = PRIMARY_METRIC
    mean_rca_f1: float
    mean_localization_f1: float
    mean_detection_score: float
    n_trials_expected: int
    n_trials_present: int
    n_success: int
    n_agent_failed: int
    token_totals: dict[str, int] = Field(default_factory=dict)
    steps_totals: dict[str, int] = Field(default_factory=dict)


class FileInventory(BaseModel):
    """Integrity for one release run + the slim submission package.

    Aligns with common leaderboard practice (HAL / Terminal-Bench): bind the
    submission to a single run identity instead of hashing every trial artifact.
    """

    model_config = ConfigDict(extra="forbid")

    source_run_sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description="SHA-256 of the official release-run run.json",
    )
    package: dict[str, str] = Field(
        default_factory=dict,
        description="SHA-256 of package-local files (metadata, README, results)",
    )
