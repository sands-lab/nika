"""JSON request/response shapes for the NIKA Remote HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PolicyMode = Literal["two_phase"]

# Append-only session logs the agent host follows while a remote trial runs.
FOLLOWED_LOGS = ("nika.jsonl", "messages.jsonl")


class HealthResponse(BaseModel):
    status: str = "ok"
    role: str = "nika-remote"


class EnvStartRequest(BaseModel):
    scenario: str
    topo_size: str | None = None
    redeploy: bool = True
    instance_tag: str | None = None
    session_tag: str | None = None
    session_id: str | None = None
    result_dir: str | None = None
    topo: str | None = None
    igp: str | None = None
    metric_strategy: str | None = None
    constant_metric: int | None = None
    bgp_mode: str | None = None
    rpki: bool | None = None
    backend: str | None = None
    device_profile: str | None = None
    static_validation: bool = False


class EnvStartResponse(BaseModel):
    session_id: str
    session: dict[str, Any] = Field(default_factory=dict)


class FailureInjectRequest(BaseModel):
    session_id: str
    problem_names: list[str]
    param_overrides: dict[str, str] = Field(default_factory=dict)
    # Benchmark root causes the injected ground truth must match.
    expected_root_causes: list[dict[str, Any]] | None = None


class FailureInjectResponse(BaseModel):
    session_id: str
    session: dict[str, Any] = Field(default_factory=dict)


class McpAttachRequest(BaseModel):
    policy_mode: PolicyMode = "two_phase"
    public_host: str | None = None


class McpAttachResponse(BaseModel):
    session_id: str
    gateway_port: int
    gateway_base_url: str
    # Host-only secret for POST /gateway/sessions/{id}/phase.
    phase_token: str = ""
    # Secret every MCP request of the session carries (X-Session-Token).
    session_token: str = ""


class SessionCloseRequest(BaseModel):
    undeploy: bool = True
    stop_all: bool = False


class SessionRebindRequest(BaseModel):
    """Give a running session a fresh identity (e.g. after a VM snapshot restore)."""

    session_id: str
    # Minted by the agent host: a restored lab host's clock is the snapshot's.
    agent_session_id: str | None = None


class SessionRebindResponse(BaseModel):
    session_id: str
    session: dict[str, Any] = Field(default_factory=dict)


class SessionLogResponse(BaseModel):
    """Complete lines appended to a session log since ``offset``."""

    data: str
    offset: int
    # Daemon wall clock when the chunk was read; lets the client record its offset.
    server_time: float


class SessionContainersResponse(BaseModel):
    session_id: str
    lab_name: str
    containers: list[dict[str, Any]] = Field(default_factory=list)


class ErrorBody(BaseModel):
    error: str
    error_type: str | None = None
