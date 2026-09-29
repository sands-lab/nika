"""Agent / judge resolution from RunConfig (CLI > YAML > defaults).

Operational settings no longer come from ``.env``. Credentials remain in ``.env``.
"""

from __future__ import annotations

import os

from nika.run_config.loader import get_run_config
from nika.run_config.schema import RunConfig
from agent.utils.provider_env import validate_provider_for_agent


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


def resolve_agent_timeout(*, config: RunConfig | None = None) -> int:
    return _cfg(config).agent.timeout_sec


def resolve_reasoning_effort(
    value: str | None = None, *, config: RunConfig | None = None
) -> str | None:
    if value is not None:
        return value
    return _cfg(config).agent.reasoning_effort


# mock has no LLM; SADE is excluded; Codex exposes no output-token cap setting.
_MAX_TOKENS_UNSUPPORTED = frozenset(
    {"mock", "community.sade", "cli.codex", "sdk.codex_sdk"}
)


def resolve_max_tokens(
    agent_type: str, *, config: RunConfig | None = None
) -> int | None:
    """Effective ``agent.max_tokens`` for *agent_type* (None when not applied)."""
    if agent_type.lower() in _MAX_TOKENS_UNSUPPORTED:
        return None
    return _cfg(config).agent.max_tokens


def resolve_agent_model(
    agent_type: str,
    model: str | None = None,
    *,
    config: RunConfig | None = None,
) -> str:
    """Resolve model id from CLI or ``agent.model``."""
    if model:
        return model

    cfg = _cfg(config)
    if yaml_model := (cfg.agent.model or "").strip():
        return yaml_model

    match agent_type.lower():
        case "cli.claude" | "sdk.claude_sdk" | "community.sade":
            raise ValueError(
                "Missing model: set agent.model in config/nika.yaml or pass -m/--model."
            )
        case "mock":
            return "mock"
        case "cli.codex" | "sdk.codex_sdk":
            raise ValueError(
                "Missing model: set agent.model in config/nika.yaml or pass -m/--model."
            )
        case "byo.langgraph" | "byo.mcp_agent" | "byo.autogen":
            raise ValueError(
                "Missing model: set agent.model in config/nika.yaml or pass -m/--model."
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
    """Export YAML ``agent.custom.base_url`` / ``model`` into ``NIKA_CUSTOM_*``.

    Used when ``provider: custom``, and as an optional Anthropic/OpenAI-compatible
    endpoint override when those providers are selected. Codex/Claude
    ``prepare_*_env`` helpers still read ``NIKA_CUSTOM_*`` from the process
    environment when mapping credentials. BYO factories prefer
    ``RunConfig.agent.custom`` or the mapped ``*_BASE_URL``. API keys stay in
    ``.env``. Empty YAML values clear leftover process env so ops env cannot win.
    """
    cfg = _cfg(config)
    base = (cfg.agent.custom.base_url or "").strip()
    model = (cfg.agent.model or "").strip()
    if base:
        os.environ["NIKA_CUSTOM_BASE_URL"] = base
        os.environ["CUSTOM_API_BASE"] = base
    else:
        os.environ.pop("NIKA_CUSTOM_BASE_URL", None)
        os.environ.pop("CUSTOM_API_BASE", None)
    if model:
        os.environ["NIKA_CUSTOM_MODEL"] = model
    else:
        os.environ.pop("NIKA_CUSTOM_MODEL", None)
