"""Credential helpers for sdk.claude_sdk."""

from __future__ import annotations

import os

from agent.cli.claude.config import (
    claude_sbx_secret_available,
    has_env_claude_credentials,
    prepare_claude_subprocess_env,
    resolve_claude_model,
)


def claude_sdk_credentials_available() -> bool:
    """Return whether Claude-compatible credentials or an sbx secret exist."""
    return has_env_claude_credentials() or claude_sbx_secret_available()


def prepare_claude_sdk_env(*, session_id: str, provider: str) -> dict[str, str]:
    """Build the Claude SDK environment for the selected provider and session."""
    if not claude_sdk_credentials_available():
        raise RuntimeError(
            "Model provider credentials are not set. Configure ANTHROPIC_API_KEY "
            "(native), DEEPSEEK_API_KEY with agent.provider=deepseek, or "
            "agent.custom.base_url with agent.provider=custom and optional "
            "NIKA_CUSTOM_API_KEY. Store keys in the repository-root .env and "
            "operational settings in config/nika.yaml. You can also authenticate "
            "with Claude `/login` "
            "so the host `anthropic` sbx secret is stored "
            "(see docs/agent-sandbox.md)."
        )
    from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL, in_sandbox

    env = prepare_claude_subprocess_env(provider=provider, agent_type="sdk.claude_sdk")
    env["NIKA_SESSION_ID"] = session_id
    if in_sandbox():
        # Inside the microVM only placeholders/sentinels are valid. Never remap
        # host DeepSeek/Anthropic secrets over a set-custom placeholder already
        # applied from runtime env.
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY", "").strip() or PROXY_MANAGED_SENTINEL
        )
        env["ANTHROPIC_API_KEY"] = api_key
        auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        if auth_token == PROXY_MANAGED_SENTINEL or auth_token.startswith("sbx-cs-"):
            env["ANTHROPIC_AUTH_TOKEN"] = auth_token
        elif api_key == PROXY_MANAGED_SENTINEL or api_key.startswith("sbx-cs-"):
            # DeepSeek Claude Code docs prefer AUTH_TOKEN; alias the placeholder.
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
        else:
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
        base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        if base:
            env["ANTHROPIC_BASE_URL"] = base
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("NIKA_CUSTOM_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
    return env


def resolve_claude_sdk_model(model: str | None) -> str:
    """Return *model* or raise when configuration did not resolve one."""
    return resolve_claude_model(model)
