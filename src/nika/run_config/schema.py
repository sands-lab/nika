"""Pydantic schema for NIKA run configuration (``config/nika.yaml``)."""

from __future__ import annotations

import re
import warnings
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from nika.agents.provider_env import validate_provider_for_agent


class RemoteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str | None = None
    artifact_root: str | None = None


# VM memory of labs that outgrow the default ``vm_memory``: k8s and SR Linux
# labs, large ISP topologies, Cisco XRd (whose build otherwise runs out of
# memory), and dc_clos (size l uses more than 4 GiB in the guest).
# ``vm_memory_overrides`` entries take precedence.
DEFAULT_VM_MEMORY_OVERRIDES = {
    "dc_clos": "8GiB",
    "k8s_lab": "8GiB",
    "llmd_lab": "8GiB",
    "min3clos": "8GiB",
    "iosxr_simple_bgp": "8GiB",
    "isp_pdh": "20GiB",
    "isp_di-yuan": "20GiB",
    "isp_dfn-gwin": "20GiB",
}


def _gib(value: str) -> str:
    if not value.endswith("GiB") or not value[:-3].isdigit():
        raise ValueError(f"VM memory must look like '8GiB', got {value!r}")
    return value


class FirecrackerSettings(BaseModel):
    """Firecracker execution (``nika.execution.mode: firecracker``), advanced.

    Each environment variant is deployed and verified once in a microVM booted
    from the base image in ``root`` and kept as a *lab image*; trials restore a
    copy of it instead of deploying from scratch. See
    docs/operations/firecracker.md.
    """

    model_config = ConfigDict(extra="forbid")

    # Directory on a reflink-capable filesystem (btrfs, XFS) for the base image,
    # lab and case images, trial copies and NIKA's Firecracker build. Relative
    # paths are under the NIKA checkout, so uninstalling NIKA removes it.
    root: str = "runtime/firecracker"
    vm_cpus: int = Field(default=4, ge=1)
    # Most Kathara labs use under 2 GiB; with huge pages, admission reserves all of it.
    vm_memory: str = "4GiB"
    # Scenario id -> VM memory, for larger labs, merged over
    # DEFAULT_VM_MEMORY_OVERRIDES. Image builds warn when a lab leaves too
    # little of its VM memory free.
    vm_memory_overrides: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_VM_MEMORY_OVERRIDES)
    )
    # Back VM memory with 2 MiB huge pages reserved on the host (vm.nr_hugepages),
    # which Firecracker execution requires. Without them (false), the guest runs
    # on 4 KiB pages and concurrent case-image restores slow the lab for seconds.
    huge_pages: bool = True
    # Re-run the startup lab verification after each restore, as a cold deploy does.
    verify_after_restore: bool = True
    # Also keep each failure case as a case image (snapshot after injection, its
    # memory stored as a compressed diff against the lab image): trials skip
    # verification and injection. All trials of a case start from the same
    # injected state. Costs disk (tens of MiB per case) and one build per case.
    case_images: bool = False
    # Delete the lab images a benchmark run used when it ends. Keeping them (the
    # default) takes substantial disk (each holds its lab's memory and disk
    # changes, often 2-8 GiB) but lets later runs skip the builds.
    clean_after_run: bool = False

    @field_validator("vm_memory")
    @classmethod
    def _memory_in_gib(cls, value: str) -> str:
        return _gib(value)

    @field_validator("vm_memory_overrides")
    @classmethod
    def _overrides_in_gib(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            scenario: _gib(memory)
            for scenario, memory in {**DEFAULT_VM_MEMORY_OVERRIDES, **value}.items()
        }


class SandboxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Sandbox runtime for sandbox-supported agents:
    # Docker Sandboxes ("sbx") or an E2B-compatible service ("e2b").
    backend: Literal["sbx", "e2b"] = "sbx"
    # e2b: API URL of a self-hosted E2B-compatible service such as AgentENV,
    # also used as its sandbox URL. Null keeps the E2B SDK defaults (E2B cloud).
    url: str | None = None
    keep: bool = False
    # Explicit null hands sizing to sbx: every host vCPU and half of host memory.
    cpus: StrictInt | None = 2
    memory: str | None = "4g"
    offline_sdk_wheels: bool = True
    upstream_proxy: str | None = None


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
    # Observation budget for MCP tool text (mini-swe-agent head/tail style).
    # Over budget: keep first/last halves and elide the middle. 0 disables.
    tool_output_max_chars: int = 10000

    @field_validator("gateway_port")
    @classmethod
    def _gateway_port_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("nika.mcp.gateway_port must be >= 0")
        return value

    @field_validator("tool_output_max_chars")
    @classmethod
    def _tool_output_chars_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("nika.mcp.tool_output_max_chars must be >= 0")
        return value


class StaticValidationSettings(BaseModel):
    """Enable the optional pre-deployment Batfish verifier."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Java heap for the Batfish container; applies when NIKA creates it.
    batfish_heap: str = Field(default="8g", pattern=r"^[1-9][0-9]*[mMgG]$")


class RuntimeValidationSettings(BaseModel):
    """Control post-deploy runtime verification depth and failure-effect checks."""

    model_config = ConfigDict(extra="forbid")

    depth: Literal["light", "full"] = "light"
    failure_effect: bool = False


class RemoteWriteSettings(BaseModel):
    """Push recorded metrics to a hosted Prometheus such as Grafana Cloud.

    Off by default. The password or API token is read from
    ``NIKA_METRICS_REMOTE_WRITE_TOKEN``.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    url: str | None = None
    username: str | None = None

    @model_validator(mode="after")
    def _url_when_enabled(self) -> RemoteWriteSettings:
        if self.enabled and not self.url:
            raise ValueError("nika.metrics.remote_write.enabled requires url")
        return self


class ShareSettings(BaseModel):
    """Public dashboard link for each benchmark run, through a Cloudflare tunnel.

    Off by default. Without ``allowed_emails``, anyone with the link can view
    the dashboard; with it, viewers confirm one of these addresses by a
    one-time code.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ttl_days: int = Field(default=7, ge=1, le=30)
    allowed_emails: list[str] = Field(default_factory=list)


class MetricsSettings(BaseModel):
    """Monitoring stack started by ``nika observability on``."""

    model_config = ConfigDict(extra="forbid")

    # How often Prometheus scrapes and cAdvisor samples, such as 15s or 30s.
    scrape_interval: str = "15s"
    remote_write: RemoteWriteSettings = Field(default_factory=RemoteWriteSettings)
    share: ShareSettings = Field(default_factory=ShareSettings)

    @field_validator("scrape_interval")
    @classmethod
    def _interval(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9][0-9]*s|1m", value) or (
            value.endswith("s") and not 5 <= int(value[:-1]) <= 60
        ):
            raise ValueError(
                f"scrape_interval must be between 5s and 60s (or 1m), got {value!r}"
            )
        return value


class ExecutionSettings(BaseModel):
    """Where benchmark trials run their labs."""

    model_config = ConfigDict(extra="forbid")

    # ``native``: on this host. ``firecracker``: in Firecracker microVMs restored
    # from lab images, configured under ``firecracker``.
    mode: Literal["native", "firecracker"] = "native"
    firecracker: FirecrackerSettings = Field(default_factory=FirecrackerSettings)


class NikaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_observability(cls, data: Any) -> Any:
        """Ignore ``nika.observability``, which saved run configs still hold.

        It configured Langfuse tracing; the model proxy now records every
        model call in ``model_calls.jsonl``. Also ignore ``nika.telemetry``:
        the management subnet is fixed in code, not a run-config field.
        """
        if not isinstance(data, dict):
            return data
        if "observability" not in data and "telemetry" not in data:
            return data
        data = dict(data)
        data.pop("telemetry", None)
        if "observability" not in data:
            return data
        observability = data.pop("observability")
        if isinstance(observability, dict) and observability.get("langfuse_enabled"):
            # A UserWarning, because Python hides DeprecationWarning by default.
            warnings.warn(
                "nika.observability is ignored: Langfuse tracing was removed; "
                "model_calls.jsonl records every model call.",
                UserWarning,
                stacklevel=2,
            )
        return data

    result_dir: str = "results"
    remote: RemoteSettings = Field(default_factory=RemoteSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
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
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)


class AgentLlmSettings(BaseModel):
    """Control request timeout, retries and streaming for the LangGraph model factory."""

    model_config = ConfigDict(extra="forbid")

    timeout_sec: float = 480.0
    max_retries: int = 2
    stream: bool = True

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


class AgentSubmissionSettings(BaseModel):
    """How the frozen diagnosis report becomes the scored submission.

    ``structured``: the agent's own model picks the answer, through the
    agent's ``submit()`` or NIKA's default one. ``unstructured``: NIKA maps
    the report with the model below, the same way for every agent type.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["structured", "unstructured"] = "structured"
    provider: Literal["openai", "anthropic", "deepseek", "custom"] | None = None
    model: str | None = None
    # OpenAI-style API root for provider=custom; defaults to agent.base_url.
    base_url: str | None = None
    # Independent mappings of the report; the majority answer is submitted.
    samples: int = 1
    # Sampling temperature of the mapping model; null keeps the provider
    # default (0 for custom). Use a value above 0 when samples > 1.
    temperature: float | None = None

    @model_validator(mode="after")
    def _mode_fields(self) -> "AgentSubmissionSettings":
        mapper_fields = (self.provider, self.model, self.base_url, self.temperature)
        if self.mode == "structured":
            if any(value is not None for value in mapper_fields) or self.samples != 1:
                raise ValueError(
                    "agent.submission provider/model/base_url/samples/temperature "
                    "apply only to mode: unstructured"
                )
        elif not self.provider or not (self.model or "").strip():
            raise ValueError(
                "agent.submission.mode unstructured requires provider and model"
            )
        if self.samples < 1:
            raise ValueError("agent.submission.samples must be >= 1")
        if self.temperature is not None and self.temperature < 0:
            raise ValueError("agent.submission.temperature must be >= 0")
        return self

    def identity(self) -> dict[str, Any]:
        """Settings that define the submission, as recorded with a run."""
        if self.mode == "structured":
            return {"mode": self.mode}
        return self.model_dump(exclude_none=True)


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "byo.langgraph"
    provider: str = "openai"
    model: str | None = None
    # Inference endpoint for provider=custom; other providers ignore it.
    # OpenAI-style API root, usually ending in /v1 (http://host:8000/v1).
    base_url: str | None = None
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
    # Output-token cap per model response. Not applied by Codex agents or SADE.
    max_tokens: int = 8192
    # Sampling. None = do not send, so the server applies the model's own
    # generation defaults. Applied by byo.langgraph and byo.mcp_agent.
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    llm: AgentLlmSettings = Field(default_factory=AgentLlmSettings)
    access: AgentAccessSettings = Field(default_factory=AgentAccessSettings)
    submission: AgentSubmissionSettings = Field(default_factory=AgentSubmissionSettings)

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_custom(cls, data: Any) -> Any:
        """Accept the old ``agent.custom.base_url`` layout used by saved configs."""
        if not isinstance(data, dict) or "custom" not in data:
            return data
        data = dict(data)
        legacy = data.pop("custom") or {}
        if not isinstance(legacy, dict):
            raise ValueError("agent.custom must be a mapping")
        unknown = set(legacy) - {"base_url"}
        if unknown:
            raise ValueError(f"unknown agent.custom keys: {sorted(unknown)}")
        if legacy.get("base_url") is not None:
            warnings.warn(
                "agent.custom.base_url is deprecated; use agent.base_url instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            data.setdefault("base_url", legacy["base_url"])
        return data

    @field_validator("max_steps")
    @classmethod
    def _max_steps_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent.max_steps must be >= 1")
        return value

    @field_validator("max_tokens")
    @classmethod
    def _max_tokens_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("agent.max_tokens must be >= 1")
        return value

    @field_validator("temperature")
    @classmethod
    def _temperature_non_negative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("agent.temperature must be >= 0")
        return value

    @field_validator("top_p")
    @classmethod
    def _top_p_in_range(cls, value: float | None) -> float | None:
        if value is not None and not 0 < value <= 1:
            raise ValueError("agent.top_p must be in (0, 1]")
        return value

    def sampling_params(self) -> dict[str, float | int]:
        """Explicitly configured sampling parameters (unset ones are omitted)."""
        params = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "seed": self.seed,
        }
        return {key: value for key, value in params.items() if value is not None}

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
    heavy_batch_size: int = 1
    case_timeout_sec: int = 2400
    continue_on_error: bool = False
    retry_passes: int = 0
    resume: bool = True
    session_tag: str | None = None

    @field_validator("batch_size", "heavy_batch_size")
    @classmethod
    def _batch_size_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator("case_timeout_sec", "retry_passes")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("must be >= 0")
        return value


class RunConfig(BaseModel):
    """Top-level run configuration."""

    model_config = ConfigDict(extra="forbid")

    # agent first: most user-edited knobs; dump order follows field order.
    agent: AgentSettings = Field(default_factory=AgentSettings)
    nika: NikaSettings = Field(default_factory=NikaSettings)
    benchmark: BenchmarkSettings = Field(default_factory=BenchmarkSettings)

    @model_validator(mode="after")
    def _validate_agent_provider(self) -> RunConfig:
        agent_type = self.agent.type.strip()
        if agent_type.lower() != "mock":
            validate_provider_for_agent(agent_type, self.agent.provider)
        return self


def default_run_config() -> RunConfig:
    return RunConfig()
