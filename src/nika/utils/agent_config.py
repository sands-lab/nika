"""Agent / judge resolution from RunConfig (CLI > YAML > defaults).

Operational settings no longer come from ``.env``. Credentials remain in ``.env``.
"""

from __future__ import annotations

import os

from nika.run_config.loader import get_run_config
from nika.run_config.schema import AgentSubmissionSettings, RunConfig
from nika.agents.registry import find_agent_spec, get_agent_spec
from nika.agents.provider_env import validate_provider_for_agent


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
    spec = find_agent_spec(agent_type)
    if spec is not None and not spec.requires_provider:
        if value:
            return validate_provider_for_agent(agent_type, value)
        return None
    raw = value or cfg.agent.provider
    if not raw:
        raise ValueError(
            "Missing agent.provider: set it in config/nika.yaml or pass -p/--provider."
        )
    return validate_provider_for_agent(agent_type, raw)


def resolve_max_steps(
    value: int | None = None, *, config: RunConfig | None = None
) -> int:
    if value is not None:
        return value
    return _cfg(config).agent.max_steps


def resolve_agent_timeout(*, config: RunConfig | None = None) -> int:
    return _cfg(config).agent.timeout_sec


def resolve_submission_settings(
    *, config: RunConfig | None = None
) -> AgentSubmissionSettings:
    return _cfg(config).agent.submission


def resolve_reasoning_effort(
    agent_type: str | None,
    value: str | None = None,
    *,
    config: RunConfig | None = None,
) -> str | None:
    """Effective reasoning effort: CLI, then ``agent.reasoning_effort``, then the
    agent's built-in default."""
    if value is not None:
        return value
    configured = _cfg(config).agent.reasoning_effort
    if configured is not None or agent_type is None:
        return configured
    spec = find_agent_spec(agent_type)
    return spec.default_reasoning_effort if spec is not None else None


def resolve_max_tokens(
    agent_type: str, *, config: RunConfig | None = None
) -> int | None:
    """Effective ``agent.max_tokens`` for *agent_type* (None when not applied)."""
    spec = find_agent_spec(agent_type)
    if spec is not None and not spec.supports_max_tokens:
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

    if default_model := get_agent_spec(agent_type).default_model:
        return default_model
    raise ValueError(
        "Missing model: set agent.model in config/nika.yaml or pass -m/--model."
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
    """Export YAML ``agent.base_url`` / ``model`` into ``NIKA_CUSTOM_*``.

    Only ``provider: custom`` uses ``agent.base_url``; other providers have
    built-in endpoints, so the value is cleared for them. The host model proxy
    reads ``NIKA_CUSTOM_*`` from the process environment when mapping
    credentials. API keys stay in ``.env``. Empty YAML values clear leftover
    process env so ops env cannot win.
    """
    cfg = _cfg(config)
    is_custom = cfg.agent.provider.strip().lower() == "custom"
    base = (cfg.agent.base_url or "").strip() if is_custom else ""
    model = (cfg.agent.model or "").strip()
    if base:
        os.environ["NIKA_CUSTOM_BASE_URL"] = base
    else:
        os.environ.pop("NIKA_CUSTOM_BASE_URL", None)
    if model:
        os.environ["NIKA_CUSTOM_MODEL"] = model
    else:
        os.environ.pop("NIKA_CUSTOM_MODEL", None)
