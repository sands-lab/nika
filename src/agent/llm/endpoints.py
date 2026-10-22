"""Model endpoint variables of the agent's environment.

An agent reaches its model only through the standard client variables:
``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` for OpenAI-compatible providers
(``openai``, ``deepseek``, ``custom``) and ``ANTHROPIC_*`` for Anthropic-style
clients. The host points them at the provider, or at its model proxy, and
holds the real keys.
"""

from __future__ import annotations

import os

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_ANTHROPIC_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
ENV_ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"
ENV_DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY"
# Claude subscription token (``claude setup-token``).
ENV_CLAUDE_CODE_OAUTH_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"

DEEPSEEK_OPENAI_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

# Key for unauthenticated OpenAI-compatible servers (client libraries require one).
UNAUTHENTICATED_API_KEY = "no-key"

SUPPORTED_PROVIDERS = ("openai", "anthropic", "deepseek", "custom")


def require_provider(provider: str | None) -> str:
    """Return the normalized provider name, or raise when it is unset."""
    name = (provider or "").strip().lower()
    if not name:
        raise ValueError("The agent has no LLM provider.")
    return name


def openai_base_url(provider: str) -> str | None:
    """Base URL of an OpenAI-compatible *provider* (``None``: client default)."""
    base = os.environ.get(ENV_OPENAI_BASE_URL, "").strip()
    if base:
        return base
    return DEEPSEEK_OPENAI_BASE_URL if provider == "deepseek" else None


def openai_api_key(provider: str) -> str | None:
    """API key of an OpenAI-compatible *provider*, if the environment has one."""
    if provider == "deepseek":
        key = os.environ.get(ENV_DEEPSEEK_API_KEY, "").strip()
        if key:
            return key
    return os.environ.get(ENV_OPENAI_API_KEY, "").strip() or None
