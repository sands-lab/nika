"""Chat model client for the models NIKA runs on the host to score answers.

The unstructured report mapper and the LLM judge use it. Their requests carry
the same provider parameters as an agent's client with the same settings:
timeout, retries, streaming with usage, and ``max_tokens``. Endpoints and keys
come from :func:`~nika.agents.provider_env.host_model_endpoint`, else from the
standard client variables.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from nika.agents.provider_env import (
    CUSTOM_UNAUTHENTICATED_API_KEY,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_BASE_URL,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    host_model_endpoint,
)

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


def load_model(
    provider: str,
    model: str,
    *,
    max_tokens: int | None = None,
    base_url: str | None = None,
) -> BaseChatModel:
    """Build the chat model for *provider* with the run's ``agent.llm`` settings.

    *base_url* overrides the endpoint of the ``custom`` provider.
    """
    from nika.run_config.loader import get_run_config

    llm = get_run_config().agent.llm
    timeout = float(llm.timeout_sec)
    retries = int(llm.max_retries)
    stream = bool(llm.stream)
    url, api_key = host_model_endpoint(provider, base_url=base_url)

    if provider in ("openai", "custom"):
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {
            "model": model,
            "timeout": timeout,
            "max_retries": retries,
        }
        url = url or os.environ.get(ENV_OPENAI_BASE_URL, "").strip() or None
        if url:
            kwargs["base_url"] = url
        api_key = api_key or os.environ.get(ENV_OPENAI_API_KEY, "").strip() or None
        if provider == "custom":
            # ChatOpenAI requires a key even for unauthenticated local servers.
            kwargs["api_key"] = api_key or CUSTOM_UNAUTHENTICATED_API_KEY
            if max_tokens is not None:
                # ChatOpenAI renames max_tokens to max_completion_tokens, which
                # not every OpenAI-compatible server accepts.
                kwargs["extra_body"] = {"max_tokens": max_tokens}
        else:
            if api_key:
                kwargs["api_key"] = api_key
            if max_tokens is not None:
                # Sent as max_completion_tokens, as OpenAI reasoning models require.
                kwargs["max_tokens"] = max_tokens
        if stream:
            # A custom base_url turns streamed usage off by default.
            kwargs.update(streaming=True, stream_usage=True)
        return ChatOpenAI(**kwargs)

    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        kwargs = {
            "model": model,
            "api_key": api_key,
            "api_base": url,
            "timeout": timeout,
            "max_retries": retries,
            "max_tokens": max_tokens,
        }
        if stream:
            kwargs.update(streaming=True, stream_usage=True)
        return ChatDeepSeek(**kwargs)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs = {
            "model": model,
            "api_key": os.getenv(ENV_ANTHROPIC_API_KEY) or None,
            "base_url": os.getenv(ENV_ANTHROPIC_BASE_URL) or None,
            "default_request_timeout": timeout,
            "max_retries": retries,
        }
        if stream:
            kwargs["streaming"] = True
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return ChatAnthropic(**kwargs)

    raise ValueError(f"Unsupported llm provider: {provider}")
