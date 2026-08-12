"""Pydantic schema for versioned NIKA run configuration (``config/nika.yaml``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nika.utils.provider_env import AGENT_PROVIDERS, validate_provider_for_agent


class RemoteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str | None = None
    artifact_root: str | None = None


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep: bool = False
    cpus: str | None = None
    memory: str | None = None
    offline_sdk_wheels: bool = False
    upstream_proxy: str | None = None


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    langfuse_enabled: bool = False
    langfuse_host: str = "https://cloud.langfuse.com"


class JudgeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-5-mini"


class K8sSettings(BaseModel):
    """How agents reach Kubernetes labs (host kubeconfig / MCP)."""

    model_config = ConfigDict(extra="forbid")

    # auto|mcp: register k8s_mcp_server for agents
    # kubectl_only: power-user host kubectl; skip k8s MCP registration
    access: str = "auto"

    @field_validator("access")
    @classmethod
    def _access_mode(cls, value: str) -> str:
        normalized = (value or "auto").strip().lower()
        allowed = {"auto", "mcp", "kubectl_only"}
        if normalized not in allowed:
            raise ValueError(f"nika.k8s.access must be one of {sorted(allowed)}")
        return normalized


class NikaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_dir: str = "results"
    enable_skills: bool = True
    remote: RemoteSettings = Field(default_factory=RemoteSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    judge: JudgeSettings = Field(default_factory=JudgeSettings)
    k8s: K8sSettings = Field(default_factory=K8sSettings)


class AgentModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    langgraph: str | None = None
    mcp_agent: str | None = None
    autogen: str | None = None
    codex: str | None = None
    codex_sdk: str | None = None
    claude: str | None = None
    claude_sdk: str | None = None
    sade: str | None = None


class CustomModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    model: str | None = None


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "byo.langgraph"
    provider: str = "openai"
    model: str | None = None
    max_steps: int = 20
    reasoning_effort: str | None = None
    models: AgentModels = Field(default_factory=AgentModels)
    custom: CustomModelSettings = Field(default_factory=CustomModelSettings)

    @field_validator("max_steps")
    @classmethod
    def _max_steps_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent.max_steps must be >= 1")
        return value


class BenchmarkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release: str | None = None
    split: str | None = None
    batch_size: int = 1
    case_timeout_sec: int = 2400
    continue_on_error: bool = False
    retry_passes: int = 0
    resume: bool = True
    session_tag: str | None = None

    @field_validator("batch_size")
    @classmethod
    def _batch_size_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("benchmark.batch_size must be >= 1")
        return value

    @field_validator("case_timeout_sec", "retry_passes")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class RunConfig(BaseModel):
    """Top-level versioned run configuration."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    nika: NikaSettings = Field(default_factory=NikaSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    benchmark: BenchmarkSettings = Field(default_factory=BenchmarkSettings)

    @model_validator(mode="after")
    def _validate_agent_provider(self) -> RunConfig:
        agent_type = self.agent.type.strip()
        if agent_type.lower() != "mock":
            validate_provider_for_agent(agent_type, self.agent.provider)
        if (
            self.agent.provider == "custom"
            and not (self.agent.custom.base_url or "").strip()
        ):
            # Allow incomplete configs at load time for templates; resolve later.
            pass
        return self

    def model_for_agent(self, agent_type: str | None = None) -> str | None:
        """Return the best model id for *agent_type* from YAML (no CLI)."""
        at = (agent_type or self.agent.type).lower()
        if self.agent.model:
            return self.agent.model
        models = self.agent.models
        match at:
            case "byo.langgraph":
                return models.langgraph
            case "byo.mcp_agent":
                return models.mcp_agent
            case "byo.autogen":
                return models.autogen
            case "cli.codex":
                return models.codex
            case "sdk.codex_sdk":
                return models.codex_sdk or models.codex
            case "cli.claude":
                return models.claude
            case "sdk.claude_sdk":
                return models.claude_sdk or models.claude
            case "community.sade":
                return models.sade or models.claude
            case _:
                return None

    def to_display_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


def default_run_config() -> RunConfig:
    return RunConfig()


def allowed_providers_hint(agent_type: str) -> str:
    allowed = AGENT_PROVIDERS.get(agent_type.lower())
    if not allowed:
        return "openai, anthropic, deepseek, custom"
    return ", ".join(sorted(allowed))
