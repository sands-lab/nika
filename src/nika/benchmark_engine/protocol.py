"""Benchmark engine API: the lifecycle operations of contract B.

The frontend drives every trial through these operations and never reaches
into a release's environment, failure, tool, or scoring code directly. The
engine also runs each trial's MCP gateway, so everything the agent observes
comes from the release. See ``docs/development/benchmark-contracts.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Engine API versions this frontend speaks (``engine.api.version`` in a release
# manifest). One version covers the manifest format, the lifecycle operations,
# and the files the engine writes. Within a version, changes are additive
# only; a breaking change adds a new version here.
API_VERSIONS: frozenset[int] = frozenset({1})
API_VERSION = max(API_VERSIONS)

# Shared secret of one run: the frontend sends it on every lifecycle request,
# so only it can drive the engine.
ENGINE_TOKEN_HEADER = "NIKA-Engine-Token"

FailureOutcome = Literal["agent_failed", "endpoint_failed", "infra_failed"]


class _Reply(BaseModel):
    # Ignore unknown fields so an engine can add reply fields without breaking
    # older frontends.
    model_config = ConfigDict(frozen=True, extra="ignore")


class _Request(BaseModel):
    # Reject unknown fields so an engine refuses a setting it cannot honor.
    model_config = ConfigDict(frozen=True, extra="forbid")


class HostInfo(_Reply):
    """Docker and kernel versions of the host that runs the environments."""

    docker: str | None = None
    kernel: str | None = None


class EngineDescription(_Reply):
    """What ``describe`` reports before a run starts."""

    api_versions: list[int]
    release_id: str
    release_version: str
    engine_commit: str | None = None
    engine_dirty: bool = False
    # Task-file name → sha256, checked against the release manifest.
    task_file_sha256: dict[str, str] = Field(default_factory=dict)
    host: HostInfo | None = None


class TaskInfo(_Reply):
    """One task of a release split, as ``list_tasks`` reports it.

    ``resource_class`` is ``light``, ``large``, ``k8s``, or ``clab``; the
    frontend admits trials by it. ``label`` is the progress label without the
    trial number.
    """

    task_id: str
    scenario: str
    topo_size: str | None = None
    resource_class: str
    faults: list[str] = Field(default_factory=list)
    label: str


class TaskList(_Reply):
    """The tasks of one release split, in split-file order.

    ``task_file_sha256`` is the hash of the split file the engine read; the
    frontend checks it against the release manifest.
    """

    release: str
    split: str
    task_file_sha256: str
    tasks: list[TaskInfo]


class TrialRequest(_Request):
    """One trial of one task.

    A release trial names its task by ``task_id`` with ``release`` and
    ``split``; the engine reads the task from its own split file and derives
    the failures, injection parameters, expected root causes, and healthy flag
    from it. A run without a release sends the full task row as ``task``
    instead, with those fields. Only the in-process engine accepts a task row.
    ``release`` also selects that release's failure variants.
    """

    task: dict[str, Any] | None = None
    task_id: str | None = None
    split: str | None = None
    failures: list[str] = Field(default_factory=list)
    inject: dict[str, Any] = Field(default_factory=dict)
    expected_root_causes: list[Any] | None = None
    healthy: bool = False
    session_id: str | None = None
    trial_dir: str | None = None
    result_dir: str | None = None
    session_tag: str | None = None
    release: str | None = None

    @model_validator(mode="after")
    def _one_task(self) -> TrialRequest:
        if (self.task is None) == (self.task_id is None):
            raise ValueError("A trial request sets exactly one of task and task_id")
        if self.task_id is not None:
            if not self.release or not self.split:
                raise ValueError("A task_id request needs release and split")
            if (
                self.failures
                or self.inject
                or self.expected_root_causes is not None
                or self.healthy
            ):
                raise ValueError(
                    "A task_id request takes its failures, injection parameters, "
                    "root causes, and healthy flag from the engine's task"
                )
        elif self.split is not None:
            raise ValueError("split applies to task_id requests only")
        return self


class Resource(_Reply):
    """A host resource the benchmark engine created for a trial."""

    kind: str
    id: str


class TrialHandle(_Reply):
    """Returned by ``start_trial``; identifies the trial in later calls."""

    session_id: str
    trial_dir: str
    resources: list[Resource] = Field(default_factory=list)

    @property
    def trial_path(self) -> Path:
        return Path(self.trial_dir)


class AgentTask(_Reply):
    """What the agent is told when its trial is ready, as ``begin`` returns it."""

    task_description: str
    scheduled: bool = Field(default=False, exclude_if=lambda value: not value)


# A trial's lifecycle as the engine service tracks it.
TrialState = Literal[
    "deploying", "deployed", "preparing", "ready", "stopping", "stopped", "failed"
]


class TrialStatus(_Reply):
    """A trial's state, as the engine service reports it over HTTP.

    ``start_trial``, ``begin``, and ``stop_trial`` answer at once with it, and
    the frontend polls ``trial_status`` until the state settles. ``handle`` is
    set once the environment is deployed, and ``task`` once the trial is
    ready. ``error``, ``error_type``, and ``outcome`` describe a ``failed``
    state.
    """

    session_id: str
    state: TrialState
    handle: TrialHandle | None = None
    task: AgentTask | None = None
    error: str | None = None
    error_type: str | None = None
    outcome: FailureOutcome | None = None


class FaultCheck(_Reply):
    """Whether the injected failure is still in place.

    ``not_applicable`` means no failure was injected (a healthy task);
    ``error`` means the engine could not read the failure's artifact.
    """

    result: Literal["present", "absent", "error", "not_applicable"]
    fault: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class AccessPolicy(_Request):
    """What the agent may touch during diagnosis; ``*`` allows everything."""

    tools: list[str] = Field(default_factory=lambda: ["*"])
    node_roles: list[str] = Field(default_factory=lambda: ["*"])
    node_ids: list[str] = Field(default_factory=list)


class AgentAccess(_Request):
    """An access policy and the name the run records it under."""

    role: str
    policy: AccessPolicy = Field(default_factory=AccessPolicy)


class GatewayRequest(_Request):
    """How the trial's MCP gateway is opened.

    ``access`` is enforced by the engine's gateway; ``None`` lets the engine
    use its own default. ``sandbox`` binds the gateway where a sandboxed agent
    reaches it, at ``agent_host``.
    """

    tool_call_limits: (
        dict[Literal["diagnosis", "submission"], Annotated[int, Field(ge=0)]] | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    access: AgentAccess | None = None
    sandbox: bool = False
    bind_host: str | None = None
    agent_host: str | None = None


class AgentEndpoints(_Reply):
    """The trial's MCP gateway (contract C), as ``agent_endpoints`` opened it.

    ``gateway_url`` is how the frontend reaches it, ``agent_url`` how the
    agent does. Every MCP request carries ``session_token``; ``phase_token``
    authorizes the frontend's phase advance and never reaches the agent.
    ``servers`` names the MCP servers the agent mounts.
    """

    gateway_url: str
    agent_url: str
    session_token: str
    phase_token: str
    servers: list[str]


class Scores(_Reply):
    """The grade of one trial, as ``score`` returns it.

    ``status`` is the trial's score status (``scored``, ``no_submission``,
    ``grading_error``, or ``infra_error``). ``metrics`` holds the release's
    score columns, null when the trial was not graded; their names belong to
    the release, not to this envelope. ``error`` says why grading failed.
    """

    status: str
    scoring_id: str
    metrics: dict[str, float | None]
    error: str | None = None


class EngineError(RuntimeError):
    """A lifecycle step failed; ``outcome`` drives retry and resume."""

    def __init__(self, message: str, *, outcome: FailureOutcome = "infra_failed"):
        super().__init__(message)
        self.outcome = outcome


class UnsupportedContractVersion(ValueError):
    """A release, engine, or run record uses a version this frontend lacks."""


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

    def list_tasks(self, release: str, split: str) -> TaskList:
        """The tasks of one release split; an engine computes their IDs."""
        ...

    def start_trial(self, request: TrialRequest) -> TrialHandle: ...

    def begin(self, handle: TrialHandle) -> AgentTask:
        """Return the agent's task once it may start; the engine decides when."""
        ...

    def schedule_operation(self, handle: TrialHandle, operation: str) -> dict: ...

    def agent_endpoints(
        self, handle: TrialHandle, request: GatewayRequest
    ) -> AgentEndpoints: ...

    def close_agent_endpoints(self, handle: TrialHandle) -> None: ...

    def fault_check(self, handle: TrialHandle) -> FaultCheck: ...

    def score(self, handle: TrialHandle, *, infra_evidence: bool = False) -> Scores:
        """Grade the trial's submission against its ground truth.

        ``infra_evidence`` reports an infrastructure failure the frontend saw;
        it turns a missing submission into ``infra_error``.
        """
        ...

    def stop_trial(self, handle: TrialHandle, *, status: str = "finished") -> None: ...
