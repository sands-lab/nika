"""Agent CLI configuration from environment variables.

No hard-coded defaults: each setting must come from a CLI flag or ``.env``.
CLI flags take precedence when both are set.
"""

from __future__ import annotations

import os

from agent.local_cli.claude_cli.config import resolve_claude_model

# Shared CLI options (nika agent run / nika benchmark run)
ENV_AGENT_TYPE = "NIKA_AGENT_TYPE"
ENV_LLM_PROVIDER = "NIKA_LLM_PROVIDER"
ENV_MAX_STEPS = "NIKA_MAX_STEPS"
ENV_MODEL = "NIKA_MODEL"

# LangGraph agent (byo/langgraph)
ENV_LANGGRAPH_MODEL = "NIKA_LANGGRAPH_MODEL"

# mcp-agent agent
ENV_MCP_AGENT_MODEL = "NIKA_MCP_AGENT_MODEL"

# SADE community agent
ENV_SADE_MODEL = "NIKA_SADE_MODEL"

# SDK agents
ENV_CLAUDE_SDK_MODEL = "NIKA_CLAUDE_SDK_MODEL"
ENV_CODEX_SDK_MODEL = "NIKA_CODEX_SDK_MODEL"

# AutoGen agent
ENV_AUTOGEN_MODEL = "NIKA_AUTOGEN_MODEL"

# Codex CLI agent
ENV_CODEX_MODEL = "NIKA_CODEX_MODEL"
ENV_CODEX_REASONING_EFFORT = "NIKA_CODEX_REASONING_EFFORT"

# LLM judge (nika eval judge)
ENV_JUDGE_PROVIDER = "NIKA_JUDGE_PROVIDER"
ENV_JUDGE_MODEL = "NIKA_JUDGE_MODEL"


def _env_str(key: str) -> str | None:
    value = os.environ.get(key, "").strip()
    return value or None


def _require_str(*, value: str | None, env_key: str, cli_flag: str) -> str:
    if value:
        return value
    if env := _env_str(env_key):
        return env
    raise ValueError(f"Missing {env_key}: set it in .env or pass {cli_flag}.")


def _require_int(*, value: int | None, env_key: str, cli_flag: str) -> int:
    if value is not None:
        return value
    if raw := _env_str(env_key):
        return int(raw)
    raise ValueError(f"Missing {env_key}: set it in .env or pass {cli_flag}.")


def resolve_agent_type(value: str | None = None) -> str:
    return _require_str(value=value, env_key=ENV_AGENT_TYPE, cli_flag="-a/--agent")


def resolve_llm_provider(value: str | None = None, *, agent_type: str) -> str | None:
    if agent_type.lower() != "byo.langgraph":
        return value
    return _require_str(value=value, env_key=ENV_LLM_PROVIDER, cli_flag="-p/--provider")


def resolve_max_steps(value: int | None = None) -> int:
    return _require_int(value=value, env_key=ENV_MAX_STEPS, cli_flag="-n/--max-steps")


def resolve_reasoning_effort(value: str | None = None) -> str | None:
    if value is not None:
        return value
    return _env_str(ENV_CODEX_REASONING_EFFORT)


def resolve_agent_model(agent_type: str, model: str | None = None) -> str:
    """Resolve model id for *agent_type*; explicit *model* wins over env."""
    if model:
        return model
    if generic := _env_str(ENV_MODEL):
        return generic

    match agent_type.lower():
        case "local_cli.claude_cli":
            return resolve_claude_model(None)
        case "community.sade":
            if sade_model := _env_str(ENV_SADE_MODEL):
                return sade_model
            return resolve_claude_model(None)
        case "sdk.claude_sdk":
            if claude_sdk_model := _env_str(ENV_CLAUDE_SDK_MODEL):
                return claude_sdk_model
            return resolve_claude_model(None)
        case "sdk.codex_sdk":
            if codex_sdk_model := _env_str(ENV_CODEX_SDK_MODEL):
                return codex_sdk_model
            return _require_str(
                value=None, env_key=ENV_CODEX_MODEL, cli_flag="-m/--model"
            )
        case "local_cli.codex_cli":
            return _require_str(
                value=None, env_key=ENV_CODEX_MODEL, cli_flag="-m/--model"
            )
        case "mock":
            return _require_str(value=None, env_key=ENV_MODEL, cli_flag="-m/--model")
        case "byo.mcp_agent":
            return _require_str(
                value=None, env_key=ENV_MCP_AGENT_MODEL, cli_flag="-m/--model"
            )
        case "byo.autogen":
            return _require_str(
                value=None, env_key=ENV_AUTOGEN_MODEL, cli_flag="-m/--model"
            )
        case "byo.langgraph":
            return _require_str(
                value=None, env_key=ENV_LANGGRAPH_MODEL, cli_flag="-m/--model"
            )
        case _:
            raise ValueError(
                f"Unsupported agent type for model resolution: {agent_type!r}"
            )


def resolve_judge_provider(value: str | None = None) -> str:
    return _require_str(
        value=value, env_key=ENV_JUDGE_PROVIDER, cli_flag="-p/--provider (judge)"
    )


def resolve_judge_model(value: str | None = None) -> str:
    return _require_str(
        value=value, env_key=ENV_JUDGE_MODEL, cli_flag="-m/--model (judge)"
    )
