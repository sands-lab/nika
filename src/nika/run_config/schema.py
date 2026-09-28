"""Pydantic schema for versioned NIKA run configuration (``config/nika.yaml``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.utils.provider_env import validate_provider_for_agent


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
    offline_sdk_wheels: bool = True
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
    apiserver: str | None = None

    @field_validator("access")
    @classmethod
    def _access_mode(cls, value: str) -> str:
        normalized = (value or "auto").strip().lower()
        allowed = {"auto", "mcp", "kubectl_only"}
        if normalized not in allowed:
            raise ValueError(f"nika.k8s.access must be one of {sorted(allowed)}")
        return normalized


class LabSettings(BaseModel):
    """Control deployment, startup, teardown, and fault verification timing."""

    model_config = ConfigDict(extra="forbid")

    deploy_attempts: int = 3
    deploy_ready_timeout_sec: float = 90.0
    deploy_settle_sec: float = 5.0
    undeploy_verify_timeout_sec: float = 30.0
    ready_max_wait_sec: float = 180.0
    ready_retry_delay_sec: float = 5.0
    failure_verify_max_attempts: int = 3
    failure_verify_retry_delay_sec: float = 5.0
    # Passed to ``clab deploy --max-workers`` to limit concurrent create/wire.
    containerlab_max_workers: int = 2

    @field_validator(
        "deploy_attempts",
        "failure_verify_max_attempts",
        "containerlab_max_workers",
    )
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator(
        "deploy_ready_timeout_sec",
        "deploy_settle_sec",
        "undeploy_verify_timeout_sec",
        "ready_max_wait_sec",
        "ready_retry_delay_sec",
        "failure_verify_retry_delay_sec",
    )
    @classmethod
    def _non_negative_float(cls, value: float) -> float:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class McpSettings(BaseModel):
    """Control MCP client timeouts and gateway binding."""

    model_config = ConfigDict(extra="forbid")

    read_timeout_sec: float = 120.0
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 0
    # Default observation budget for MCP tool text returned to the agent.
    tool_output_max_chars: int = 16384
    # Hard cap when the agent passes full=true (0 = no second cap).
    tool_output_full_max_chars: int = 100000

    @field_validator("gateway_port")
    @classmethod
    def _gateway_port_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("nika.mcp.gateway_port must be >= 0")
        return value

    @field_validator("tool_output_max_chars", "tool_output_full_max_chars")
    @classmethod
    def _tool_output_chars_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("nika.mcp tool output char limits must be >= 0")
        return value


class StaticValidationSettings(BaseModel):
    """Enable the optional pre-deployment Batfish verifier."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class RuntimeValidationSettings(BaseModel):
    """Control post-deploy runtime verification depth and failure-effect checks."""

    model_config = ConfigDict(extra="forbid")

    depth: Literal["light", "full"] = "light"
    failure_effect: bool = False


class NikaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_dir: str = "results"
    remote: RemoteSettings = Field(default_factory=RemoteSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    judge: JudgeSettings = Field(default_factory=JudgeSettings)
    k8s: K8sSettings = Field(default_factory=K8sSettings)
    lab: LabSettings = Field(default_factory=LabSettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
    static_validation: StaticValidationSettings = Field(
        default_factory=StaticValidationSettings
    )
    runtime_validation: RuntimeValidationSettings = Field(
        default_factory=RuntimeValidationSettings
    )


class AgentModels(BaseModel):
    """Legacy per-agent model fields; prefer ``agent.model`` in new YAML."""

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


class AgentLlmSettings(BaseModel):
    """Control request timeout and retries for the LangGraph model factory."""

    model_config = ConfigDict(extra="forbid")

    timeout_sec: float = 480.0
    max_retries: int = 2

    @field_validator("timeout_sec")
    @classmethod
    def _timeout_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("agent.llm.timeout_sec must be >= 0")
        return value

    @field_validator("max_retries")
    @classmethod
    def _retries_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("agent.llm.max_retries must be >= 0")
        return value


class DiagnosisAccessPolicy(BaseModel):
    """Portable diagnosis permissions selected by an agent role."""

    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=lambda: ["*"])
    node_roles: list[str] = Field(default_factory=lambda: ["*"])
    node_ids: list[str] = Field(default_factory=list)


class AgentAccessSettings(BaseModel):
    """Role-selected, execution-enforced access for the diagnosis phase."""

    model_config = ConfigDict(extra="forbid")

    role: str = "default"
    roles: dict[str, DiagnosisAccessPolicy] = Field(
        default_factory=lambda: {"default": DiagnosisAccessPolicy()}
    )

    @model_validator(mode="after")
    def _configured_role_exists(self) -> "AgentAccessSettings":
        if self.role not in self.roles:
            raise ValueError(f"agent.access.role {self.role!r} is not defined")
        return self


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "byo.langgraph"
    provider: str = "openai"
    # Canonical model id for the active agent type (see agent.models.* for legacy YAML).
    model: str | None = None
    max_steps: int = 20
    # Wall-clock budget for one agent run (diagnosis + submission), all agent types.
    # 0 disables it. Keep it below benchmark.case_timeout_sec so the agent stops
    # and cleans up before the case worker is killed.
    timeout_sec: int = 1800
    # Load shared troubleshooting skills for Claude and Codex agents.
    enable_skills: bool = True
    # Consecutive submit() validation failures before a final rejection (0 = unlimited).
    submit_reject_limit: int = 5
    reasoning_effort: str | None = None
    models: AgentModels = Field(default_factory=AgentModels)
    custom: CustomModelSettings = Field(default_factory=CustomModelSettings)
    llm: AgentLlmSettings = Field(default_factory=AgentLlmSettings)
    access: AgentAccessSettings = Field(default_factory=AgentAccessSettings)

    @field_validator("max_steps")
    @classmethod
    def _max_steps_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent.max_steps must be >= 1")
        return value

    @field_validator("timeout_sec")
    @classmethod
    def _timeout_sec_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("agent.timeout_sec must be >= 0")
        return value

    @field_validator("submit_reject_limit")
    @classmethod
    def _submit_reject_limit_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("agent.submit_reject_limit must be >= 0")
        return value


class BenchmarkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release: str | None = None
    split: str | None = None
    batch_size: int = 1
    serialize_heavy: bool = True
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
    # agent first: most user-edited knobs; dump order follows field order.
    agent: AgentSettings = Field(default_factory=AgentSettings)
    nika: NikaSettings = Field(default_factory=NikaSettings)
    benchmark: BenchmarkSettings = Field(default_factory=BenchmarkSettings)

    @model_validator(mode="before")
    @classmethod
    def _migrate_nika_enable_skills(cls, data: Any) -> Any:
        """Accept legacy ``nika.enable_skills`` as ``agent.enable_skills``."""
        if not isinstance(data, dict):
            return data
        nika = data.get("nika")
        if not isinstance(nika, dict) or "enable_skills" not in nika:
            return data
        skills = nika.pop("enable_skills")
        agent = data.get("agent")
        if not isinstance(agent, dict):
            agent = {}
            data["agent"] = agent
        agent.setdefault("enable_skills", skills)
        return data

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
        """Return model from ``agent.model`` or legacy ``agent.models.*`` (no CLI)."""
        if self.agent.model:
            return self.agent.model
        return self.legacy_model_for_agent(agent_type)

    def legacy_model_for_agent(self, agent_type: str | None = None) -> str | None:
        """Return legacy ``agent.models.*`` for *agent_type* (ignores ``agent.model``)."""
        at = (agent_type or self.agent.type).lower()
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
        """Effective config for ``nika config show`` (new agent.model shape).

        Resolves ``agent.model`` from the canonical field or legacy
        ``agent.models.*``, and omits the deprecated ``models`` map so the
        dump matches the post-convergence YAML style.
        """
        data = self.model_dump(mode="python")
        agent = data.get("agent")
        if isinstance(agent, dict):
            agent["model"] = self.model_for_agent()
            agent.pop("models", None)
        return data


def default_run_config() -> RunConfig:
    return RunConfig()
