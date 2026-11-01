"""Benchmark engine lifecycle protocol (contract B).

The frontend drives every trial through these operations and never reaches
into a release's environment, failure, or scoring code directly. See
``docs/development/benchmark-contracts.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# Contract versions this frontend speaks. Within a version, changes are
# additive only; a breaking change adds a new version here.
MANIFEST_VERSIONS: frozenset[int] = frozenset({1})
PROTOCOL_VERSIONS: frozenset[int] = frozenset({1})
RESULTS_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

PROTOCOL_VERSION = max(PROTOCOL_VERSIONS)

FailureOutcome = Literal["agent_failed", "endpoint_failed", "infra_failed"]


class _Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EngineDescription(_Message):
    """What ``describe`` reports before a run starts."""

    protocol_versions: list[int]
    release_id: str
    release_version: str
    engine_commit: str | None = None
    engine_dirty: bool = False
    # Task-file name → sha256, checked against the release manifest.
    task_file_sha256: dict[str, str] = Field(default_factory=dict)


class TrialRequest(_Message):
    """One trial of one task.

    ``task`` is the task row as the split file defines it. A pinned benchmark
    engine checks it against its own copy; the current NIKA's engine deploys it.
    """

    task: dict[str, Any]
    failures: list[str]
    inject: dict[str, Any] = Field(default_factory=dict)
    expected_root_causes: list[Any] | None = None
    healthy: bool = False
    session_id: str | None = None
    trial_dir: str | None = None
    result_dir: str | None = None
    session_tag: str | None = None


class Resource(_Message):
    """A host resource the benchmark engine created for a trial."""

    kind: str
    id: str


class TrialHandle(_Message):
    """Returned by ``start_trial``; identifies the trial in later calls."""

    session_id: str
    trial_dir: str
    resources: list[Resource] = Field(default_factory=list)
    # The environment was restored with the failure already injected.
    preinjected: bool = False

    @property
    def trial_path(self) -> Path:
        return Path(self.trial_dir)


class InjectResult(_Message):
    """``ground_truth`` is true once the trial can be scored."""

    ground_truth: bool


class FaultCheck(_Message):
    """Whether the injected failure is still in place.

    ``not_applicable`` means no failure was injected (a healthy task);
    ``error`` means the engine could not read the failure's artifact.
    """

    result: Literal["present", "absent", "error", "not_applicable"]
    fault: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class AgentEndpoints(_Message):
    """The tool surface of one trial (contract C).

    ``diagnosis_servers`` is ``None`` when every mounted diagnosis server is
    allowed. ``node_roles`` maps device names to the roles that access
    policies select on.
    """

    diagnosis_servers: list[str] | None
    node_roles: dict[str, str] = Field(default_factory=dict)


class EngineError(RuntimeError):
    """A lifecycle step failed; ``outcome`` drives retry and resume."""

    def __init__(self, message: str, *, outcome: FailureOutcome = "infra_failed"):
        super().__init__(message)
        self.outcome = outcome


class UnsupportedContractVersion(ValueError):
    """A manifest or benchmark engine speaks a contract version this frontend lacks."""


def require_supported(contract: str, version: int, supported: frozenset[int]) -> None:
    """Raise unless *version* of *contract* is one this frontend speaks."""
    if version not in supported:
        raise UnsupportedContractVersion(
            f"{contract} version {version} is not supported by this NIKA "
            f"(supported: {', '.join(str(v) for v in sorted(supported))}). "
            "Upgrade NIKA to run this release."
        )


class LifecycleProtocol(Protocol):
    """Lifecycle operations of contract B."""

    def describe(self, release: str | None = None) -> EngineDescription: ...

    def start_trial(self, request: TrialRequest) -> TrialHandle: ...

    def inject(self, handle: TrialHandle, request: TrialRequest) -> InjectResult: ...

    def agent_endpoints(self, handle: TrialHandle) -> AgentEndpoints: ...

    def fault_check(self, handle: TrialHandle) -> FaultCheck: ...

    def score(
        self,
        handle: TrialHandle,
        *,
        result_dir: str | None = None,
        infra_evidence: bool = False,
    ) -> None: ...

    def stop_trial(self, handle: TrialHandle, *, status: str = "finished") -> None: ...
