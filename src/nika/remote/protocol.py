"""JSON request/response shapes for the NIKA Remote HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PolicyMode = Literal["two_phase", "unified"]


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


class EnvStartResponse(BaseModel):
    session_id: str
    session: dict[str, Any] = Field(default_factory=dict)


class FailureInjectRequest(BaseModel):
    session_id: str
    problem_names: list[str]
    param_overrides: dict[str, str] = Field(default_factory=dict)


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


class SessionCloseRequest(BaseModel):
    undeploy: bool = True
    stop_all: bool = False


class SessionContainersResponse(BaseModel):
    session_id: str
    lab_name: str
    containers: list[dict[str, Any]] = Field(default_factory=list)


class SessionIdBody(BaseModel):
    session_id: str | None = None


class ErrorBody(BaseModel):
    error: str
    error_type: str | None = None
