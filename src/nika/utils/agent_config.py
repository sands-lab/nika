"""Agent / judge resolution from RunConfig (CLI > YAML > defaults).

Operational settings no longer come from ``.env``. Credentials remain in ``.env``.
"""

from __future__ import annotations

import logging
import os

from agent.cli.claude.config import resolve_claude_model
from nika.run_config.loader import get_run_config
from nika.run_config.schema import RunConfig
from nika.utils.provider_env import validate_provider_for_agent

logger = logging.getLogger(__name__)

# Kept as string constants for CLI help / migrate docs (not read from env for ops).
ENV_AGENT_TYPE = "NIKA_AGENT_TYPE"
ENV_LLM_PROVIDER = "NIKA_LLM_PROVIDER"
ENV_MAX_STEPS = "NIKA_MAX_STEPS"
ENV_MODEL = "NIKA_MODEL"
ENV_CLAUDE_MODEL = "NIKA_CLAUDE_MODEL"
ENV_LANGGRAPH_MODEL = "NIKA_LANGGRAPH_MODEL"
ENV_MCP_AGENT_MODEL = "NIKA_MCP_AGENT_MODEL"
ENV_SADE_MODEL = "NIKA_SADE_MODEL"
ENV_CLAUDE_SDK_MODEL = "NIKA_CLAUDE_SDK_MODEL"
ENV_CODEX_SDK_MODEL = "NIKA_CODEX_SDK_MODEL"
ENV_AUTOGEN_MODEL = "NIKA_AUTOGEN_MODEL"
ENV_CODEX_MODEL = "NIKA_CODEX_MODEL"
ENV_CODEX_REASONING_EFFORT = "NIKA_CODEX_REASONING_EFFORT"
ENV_JUDGE_PROVIDER = "NIKA_JUDGE_PROVIDER"
ENV_JUDGE_MODEL = "NIKA_JUDGE_MODEL"


def _cfg(config: RunConfig | None) -> RunConfig:
    return config if config is not None else get_run_config()


def resolve_agent_type(
    value: str | None = None, *, config: RunConfig | None = None
) -> str:
    if value:
        return value
    return _cfg(config).agent.type


def resolve_llm_provider(
    value: str | None = None,
    *,
    agent_type: str,
    config: RunConfig | None = None,
) -> str | None:
    cfg = _cfg(config)
    normalized = agent_type.lower()
    if normalized == "mock":
        if value:
            return validate_provider_for_agent(normalized, value)
        return None
    raw = value or cfg.agent.provider
    if not raw:
        raise ValueError(
            "Missing agent.provider: set it in config/nika.yaml or pass -p/--provider."
        )
    return validate_provider_for_agent(normalized, raw)


def resolve_max_steps(
    value: int | None = None, *, config: RunConfig | None = None
) -> int:
    if value is not None:
        return value
    return _cfg(config).agent.max_steps


def resolve_reasoning_effort(
    value: str | None = None, *, config: RunConfig | None = None
) -> str | None:
    if value is not None:
        return value
    return _cfg(config).agent.reasoning_effort


def resolve_agent_model(
    agent_type: str,
    model: str | None = None,
    *,
    llm_provider: str | None = None,
    config: RunConfig | None = None,
) -> str:
    """Resolve model id: CLI ``-m`` → YAML agent.model / models.* / custom.model."""
    if model:
        return model

    cfg = _cfg(config)
    provider = (llm_provider or cfg.agent.provider or "").strip().lower()

    if yaml_model := cfg.model_for_agent(agent_type):
        return yaml_model

    if provider == "custom" and (custom := (cfg.agent.custom.model or "").strip()):
        return custom

    match agent_type.lower():
        case "cli.claude" | "sdk.claude_sdk" | "community.sade":
            # Fall back to Anthropic model chain env only for Claude Code
            # compatibility inside sandboxes that still set ANTHROPIC_MODEL.
            try:
                return resolve_claude_model(None)
            except ValueError as exc:
                raise ValueError(
                    "Missing model: set agent.models.claude (or agent.model) in "
                    "config/nika.yaml or pass -m/--model."
                ) from exc
        case "mock":
            raise ValueError("Missing model for mock agent: pass -m/--model.")
        case "cli.codex" | "sdk.codex_sdk":
            raise ValueError(
                "Missing model: set agent.models.codex in config/nika.yaml "
                "or pass -m/--model."
            )
        case "byo.langgraph":
            raise ValueError(
                "Missing model: set agent.models.langgraph in config/nika.yaml "
                "or pass -m/--model."
            )
        case "byo.mcp_agent":
            raise ValueError(
                "Missing model: set agent.models.mcp_agent in config/nika.yaml "
                "or pass -m/--model."
            )
        case "byo.autogen":
            raise ValueError(
                "Missing model: set agent.models.autogen in config/nika.yaml "
                "or pass -m/--model."
            )
        case _:
            raise ValueError(
                f"Unsupported agent type for model resolution: {agent_type!r}"
            )


def resolve_judge_provider(
    value: str | None = None, *, config: RunConfig | None = None
) -> str:
    if value:
        return value
    return _cfg(config).nika.judge.provider


def resolve_judge_model(
    value: str | None = None, *, config: RunConfig | None = None
) -> str:
    if value:
        return value
    return _cfg(config).nika.judge.model


def apply_custom_provider_env(config: RunConfig | None = None) -> None:
    """Export custom base_url/model from YAML into process env for libraries.

    Only sets non-secret custom endpoint fields; API key stays in ``.env``.
    Empty YAML values clear leftover process env so ops env cannot win.
    """
    cfg = _cfg(config)
    base = (cfg.agent.custom.base_url or "").strip()
    model = (cfg.agent.custom.model or "").strip()
    if base:
        os.environ["NIKA_CUSTOM_BASE_URL"] = base
        # Compat for older readers during transition inside the same process
        os.environ["CUSTOM_API_BASE"] = base
    else:
        os.environ.pop("NIKA_CUSTOM_BASE_URL", None)
        os.environ.pop("CUSTOM_API_BASE", None)
    if model:
        os.environ["NIKA_CUSTOM_MODEL"] = model
    else:
        os.environ.pop("NIKA_CUSTOM_MODEL", None)
