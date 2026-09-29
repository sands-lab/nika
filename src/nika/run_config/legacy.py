"""Detect and map legacy operational environment variables."""

from __future__ import annotations

from typing import Any

# Operational keys that used to live in .env and must move to config/nika.yaml
LEGACY_OPERATIONAL_ENV_KEYS: tuple[str, ...] = (
    "NIKA_AGENT_TYPE",
    "NIKA_LLM_PROVIDER",
    "NIKA_MAX_STEPS",
    "NIKA_MODEL",
    "NIKA_CLAUDE_MODEL",
    "NIKA_LANGGRAPH_MODEL",
    "NIKA_MCP_AGENT_MODEL",
    "NIKA_AUTOGEN_MODEL",
    "NIKA_CODEX_MODEL",
    "NIKA_CODEX_SDK_MODEL",
    "NIKA_CLAUDE_SDK_MODEL",
    "NIKA_SADE_MODEL",
    "NIKA_CODEX_REASONING_EFFORT",
    "NIKA_RESULT_DIR",
    "NIKA_ENABLE_SKILLS",
    "NIKA_JUDGE_PROVIDER",
    "NIKA_JUDGE_MODEL",
    "NIKA_LANGFUSE_ENABLED",
    "LANGFUSE_HOST",
    "NIKA_SANDBOX_KEEP",
    "NIKA_SANDBOX_CPUS",
    "NIKA_SANDBOX_MEMORY",
    "NIKA_SANDBOX_OFFLINE_SDK_WHEELS",
    "NIKA_SANDBOX_UPSTREAM_PROXY",
    "NIKA_REMOTE_ENABLED",
    "NIKA_REMOTE_URL",
    "NIKA_REMOTE_ARTIFACT_ROOT",
    "NIKA_CUSTOM_BASE_URL",
    "NIKA_CUSTOM_MODEL",
    "CUSTOM_API_BASE",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "NIKA_CASE_TIMEOUT",
    "NIKA_CONTINUE_ON_ERROR",
    "NIKA_RETRY_PASSES",
    "NIKA_LLM_TIMEOUT",
    "NIKA_LLM_RETRIES",
    "NIKA_MCP_READ_TIMEOUT",
    "NIKA_MCP_GATEWAY_HOST",
    "NIKA_MCP_GATEWAY_PORT",
    "NIKA_K8S_ACCESS",
    "NIKA_K8S_APISERVER",
    "NIKA_DEPLOY_ATTEMPTS",
    "NIKA_DEPLOY_READY_TIMEOUT",
    "NIKA_DEPLOY_SETTLE",
    "NIKA_UNDEPLOY_VERIFY_TIMEOUT",
    "NIKA_LAB_VERIFY_MAX_WAIT",
    "NIKA_LAB_VERIFY_RETRY_DELAY",
    "NIKA_VERIFY_MAX_ATTEMPTS",
    "NIKA_VERIFY_RETRY_DELAY_SEC",
)

# Removed entirely — do not migrate values
REMOVED_ENV_KEYS: tuple[str, ...] = (
    "NIKA_SANDBOX_ENV_FILE",
    "NIKA_REMOTE_TOKEN",
)

CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "NIKA_CUSTOM_API_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "HF_TOKEN",
)


def _present(keys: tuple[str, ...], environ: dict[str, str]) -> list[str]:
    found: list[str] = []
    for key in keys:
        value = (environ.get(key) or "").strip()
        if value:
            found.append(key)
    return found


def detect_legacy_operational_env(environ: dict[str, str]) -> list[str]:
    return _present(LEGACY_OPERATIONAL_ENV_KEYS, environ)


def detect_removed_env(environ: dict[str, str]) -> list[str]:
    return _present(REMOVED_ENV_KEYS, environ)


def legacy_env_to_partial_dict(
    environ: dict[str, str],
    *,
    agent_type: str = "byo.langgraph",
    provider: str = "openai",
) -> dict[str, Any]:
    """Map legacy .env operational keys into a partial RunConfig dict."""

    def g(key: str) -> str | None:
        value = (environ.get(key) or "").strip()
        return value or None

    def g_bool(key: str) -> bool | None:
        raw = g(key)
        if raw is None:
            return None
        return raw.lower() in ("1", "true", "yes", "on")

    def g_int(key: str) -> int | None:
        raw = g(key)
        if raw is None:
            return None
        return int(raw)

    def g_float(key: str) -> float | None:
        raw = g(key)
        if raw is None:
            return None
        return float(raw)

    agent: dict[str, Any] = {}
    custom: dict[str, Any] = {}
    llm: dict[str, Any] = {}
    nika: dict[str, Any] = {}
    remote: dict[str, Any] = {}
    sandbox: dict[str, Any] = {}
    observability: dict[str, Any] = {}
    judge: dict[str, Any] = {}
    k8s: dict[str, Any] = {}
    lab: dict[str, Any] = {}
    mcp: dict[str, Any] = {}
    benchmark: dict[str, Any] = {}

    if v := g("NIKA_AGENT_TYPE"):
        agent["type"] = v
    if v := g("NIKA_LLM_PROVIDER"):
        agent["provider"] = v
    if v := g_int("NIKA_MAX_STEPS"):
        agent["max_steps"] = v
    if v := g("NIKA_MODEL"):
        agent["model"] = v
    if v := g("NIKA_CODEX_REASONING_EFFORT"):
        agent["reasoning_effort"] = v

    if "model" not in agent:
        model_keys = {
            "byo.langgraph": ("NIKA_LANGGRAPH_MODEL",),
            "byo.mcp_agent": ("NIKA_MCP_AGENT_MODEL",),
            "byo.autogen": ("NIKA_AUTOGEN_MODEL",),
            "cli.codex": ("NIKA_CODEX_MODEL",),
            "sdk.codex_sdk": ("NIKA_CODEX_SDK_MODEL", "NIKA_CODEX_MODEL"),
            "cli.claude": ("NIKA_CLAUDE_MODEL", "ANTHROPIC_MODEL"),
            "sdk.claude_sdk": (
                "NIKA_CLAUDE_SDK_MODEL",
                "NIKA_CLAUDE_MODEL",
                "ANTHROPIC_MODEL",
            ),
            "community.sade": (
                "NIKA_SADE_MODEL",
                "NIKA_CLAUDE_MODEL",
                "ANTHROPIC_MODEL",
            ),
        }
        selected_type = agent.get("type", agent_type)
        selected_provider = agent.get("provider", provider)
        candidates = (
            ("NIKA_CUSTOM_MODEL",) if selected_provider == "custom" else ()
        ) + model_keys.get(selected_type, ())
        for key in candidates:
            if v := g(key):
                agent["model"] = v
                break

    if v := g("NIKA_CUSTOM_BASE_URL") or g("CUSTOM_API_BASE"):
        custom["base_url"] = v

    if (f := g_float("NIKA_LLM_TIMEOUT")) is not None:
        llm["timeout_sec"] = f
    if (i := g_int("NIKA_LLM_RETRIES")) is not None:
        llm["max_retries"] = i

    if v := g("NIKA_RESULT_DIR"):
        nika["result_dir"] = v
    if (b := g_bool("NIKA_ENABLE_SKILLS")) is not None:
        agent["enable_skills"] = b

    if v := g("NIKA_JUDGE_PROVIDER"):
        judge["provider"] = v
    if v := g("NIKA_JUDGE_MODEL"):
        judge["model"] = v

    if (b := g_bool("NIKA_LANGFUSE_ENABLED")) is not None:
        observability["langfuse_enabled"] = b
    if v := g("LANGFUSE_HOST"):
        observability["langfuse_host"] = v

    if (b := g_bool("NIKA_SANDBOX_KEEP")) is not None:
        sandbox["keep"] = b
    if v := g("NIKA_SANDBOX_CPUS"):
        sandbox["cpus"] = v
    if v := g("NIKA_SANDBOX_MEMORY"):
        sandbox["memory"] = v
    if (b := g_bool("NIKA_SANDBOX_OFFLINE_SDK_WHEELS")) is not None:
        sandbox["offline_sdk_wheels"] = b
    if v := g("NIKA_SANDBOX_UPSTREAM_PROXY"):
        sandbox["upstream_proxy"] = v

    if (b := g_bool("NIKA_REMOTE_ENABLED")) is not None:
        remote["enabled"] = b
    if v := g("NIKA_REMOTE_URL"):
        remote["url"] = v
    if v := g("NIKA_REMOTE_ARTIFACT_ROOT"):
        remote["artifact_root"] = v

    if v := g("NIKA_K8S_ACCESS"):
        k8s["access"] = v
    if v := g("NIKA_K8S_APISERVER"):
        k8s["apiserver"] = v

    if (i := g_int("NIKA_DEPLOY_ATTEMPTS")) is not None:
        lab["deploy_attempts"] = i
    if (f := g_float("NIKA_DEPLOY_READY_TIMEOUT")) is not None:
        lab["deploy_ready_timeout_sec"] = f
    if (f := g_float("NIKA_DEPLOY_SETTLE")) is not None:
        lab["deploy_settle_sec"] = f
    if (f := g_float("NIKA_UNDEPLOY_VERIFY_TIMEOUT")) is not None:
        lab["undeploy_verify_timeout_sec"] = f
    if (f := g_float("NIKA_LAB_VERIFY_MAX_WAIT")) is not None:
        lab["ready_max_wait_sec"] = f
    if (f := g_float("NIKA_LAB_VERIFY_RETRY_DELAY")) is not None:
        lab["ready_retry_delay_sec"] = f
    if (i := g_int("NIKA_VERIFY_MAX_ATTEMPTS")) is not None:
        lab["failure_verify_max_attempts"] = i
    if (f := g_float("NIKA_VERIFY_RETRY_DELAY_SEC")) is not None:
        lab["failure_verify_retry_delay_sec"] = f

    if (f := g_float("NIKA_MCP_READ_TIMEOUT")) is not None:
        mcp["read_timeout_sec"] = f
    if v := g("NIKA_MCP_GATEWAY_HOST"):
        mcp["gateway_host"] = v
    if (i := g_int("NIKA_MCP_GATEWAY_PORT")) is not None:
        mcp["gateway_port"] = i

    if v := g_int("NIKA_CASE_TIMEOUT"):
        benchmark["case_timeout_sec"] = v
    if (b := g_bool("NIKA_CONTINUE_ON_ERROR")) is not None:
        benchmark["continue_on_error"] = b
    if v := g_int("NIKA_RETRY_PASSES"):
        benchmark["retry_passes"] = v

    if custom:
        agent["custom"] = custom
    if llm:
        agent["llm"] = llm
    if remote:
        nika["remote"] = remote
    if sandbox:
        nika["sandbox"] = sandbox
    if observability:
        nika["observability"] = observability
    if judge:
        nika["judge"] = judge
    if k8s:
        nika["k8s"] = k8s
    if lab:
        nika["lab"] = lab
    if mcp:
        nika["mcp"] = mcp

    out: dict[str, Any] = {}
    if agent:
        out["agent"] = agent
    if nika:
        out["nika"] = nika
    if benchmark:
        out["benchmark"] = benchmark
    return out
