"""Detect and map legacy operational environment variables."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

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
    "CUSTOM_API_KEY",  # custom key stays as NIKA_CUSTOM_API_KEY; CUSTOM_API_KEY is deprecated alias
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "NIKA_CASE_TIMEOUT",
    "NIKA_CONTINUE_ON_ERROR",
    "NIKA_RETRY_PASSES",
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
)

_warned = False


def _present(keys: tuple[str, ...], environ: dict[str, str] | None = None) -> list[str]:
    src = environ if environ is not None else os.environ
    found: list[str] = []
    for key in keys:
        value = (src.get(key) or "").strip()
        if value:
            found.append(key)
    return found


def detect_legacy_operational_env(
    environ: dict[str, str] | None = None,
) -> list[str]:
    return _present(LEGACY_OPERATIONAL_ENV_KEYS, environ)


def detect_removed_env(environ: dict[str, str] | None = None) -> list[str]:
    return _present(REMOVED_ENV_KEYS, environ)


def warn_legacy_operational_env(environ: dict[str, str] | None = None) -> None:
    """Emit a one-shot warning if operational settings remain in the environment.

    Values are **not** applied (CLI > YAML > defaults only).
    """
    global _warned
    if _warned:
        return
    _warned = True
    # Bridge keys are exported from YAML via apply_custom_provider_env(); ignore them
    # so a later warn after injection does not false-positive.
    bridge = frozenset({"NIKA_CUSTOM_BASE_URL", "NIKA_CUSTOM_MODEL", "CUSTOM_API_BASE"})
    legacy = [k for k in detect_legacy_operational_env(environ) if k not in bridge]
    removed = detect_removed_env(environ)
    if not legacy and not removed:
        return
    parts: list[str] = []
    if legacy:
        parts.append(
            "Operational settings in .env are ignored. Move them to config/nika.yaml "
            f"via `nika config migrate`. Detected: {', '.join(legacy)}"
        )
    if removed:
        parts.append(
            "These variables were removed and have no effect: "
            f"{', '.join(removed)}. "
            "Sandbox always uses the repo-root .env; remote no longer uses a token."
        )
    message = " ".join(parts)
    logger.warning(message)
    print(f"WARNING: {message}", flush=True)


def legacy_env_to_partial_dict(environ: dict[str, str]) -> dict[str, Any]:
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

    agent: dict[str, Any] = {}
    models: dict[str, Any] = {}
    custom: dict[str, Any] = {}
    nika: dict[str, Any] = {}
    remote: dict[str, Any] = {}
    sandbox: dict[str, Any] = {}
    observability: dict[str, Any] = {}
    judge: dict[str, Any] = {}
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

    model_map = {
        "NIKA_LANGGRAPH_MODEL": "langgraph",
        "NIKA_MCP_AGENT_MODEL": "mcp_agent",
        "NIKA_AUTOGEN_MODEL": "autogen",
        "NIKA_CODEX_MODEL": "codex",
        "NIKA_CODEX_SDK_MODEL": "codex_sdk",
        "NIKA_CLAUDE_MODEL": "claude",
        "NIKA_CLAUDE_SDK_MODEL": "claude_sdk",
        "NIKA_SADE_MODEL": "sade",
        "ANTHROPIC_MODEL": "claude",
    }
    for env_key, field in model_map.items():
        if v := g(env_key):
            models.setdefault(field, v)

    if v := g("NIKA_CUSTOM_BASE_URL") or g("CUSTOM_API_BASE"):
        custom["base_url"] = v
    if v := g("NIKA_CUSTOM_MODEL"):
        custom["model"] = v

    if v := g("NIKA_RESULT_DIR"):
        nika["result_dir"] = v
    if (b := g_bool("NIKA_ENABLE_SKILLS")) is not None:
        nika["enable_skills"] = b

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

    if v := g_int("NIKA_CASE_TIMEOUT"):
        benchmark["case_timeout_sec"] = v
    if (b := g_bool("NIKA_CONTINUE_ON_ERROR")) is not None:
        benchmark["continue_on_error"] = b
    if v := g_int("NIKA_RETRY_PASSES"):
        benchmark["retry_passes"] = v

    if models:
        agent["models"] = models
    if custom:
        agent["custom"] = custom
    if agent:
        pass  # keep
    if remote:
        nika["remote"] = remote
    if sandbox:
        nika["sandbox"] = sandbox
    if observability:
        nika["observability"] = observability
    if judge:
        nika["judge"] = judge

    out: dict[str, Any] = {"version": 1}
    if agent:
        out["agent"] = agent
    if nika:
        out["nika"] = nika
    if benchmark:
        out["benchmark"] = benchmark
    return out
