import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from agent.utils.provider_env import (
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_BASE_URL,
    ENV_CUSTOM_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    resolve_custom_api_key,
    resolve_custom_base_url,
)

load_dotenv()

# A request with no client-side timeout can block an agent step forever on a
# half-dead connection. Override per deployment via env.
LLM_TIMEOUT_SECONDS = float(os.getenv("NIKA_LLM_TIMEOUT", "300"))
LLM_MAX_RETRIES = int(os.getenv("NIKA_LLM_RETRIES", "2"))


def load_model(
    llm_provider: str = "openai", model: str = "gpt-5-mini"
) -> BaseChatModel:
    if llm_provider == "openai":
        return ChatOpenAI(
            model_name=model,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )

    if llm_provider == "deepseek":
        return ChatDeepSeek(
            model=model,
            api_key=os.getenv(ENV_DEEPSEEK_API_KEY) or None,
            base_url=DEEPSEEK_OPENAI_BASE_URL,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )

    if llm_provider == "custom":
        base_url = resolve_custom_base_url()
        if not base_url:
            raise ValueError(
                f"Missing {ENV_CUSTOM_BASE_URL}: set it in .env for custom provider "
                f"(deprecated alias: CUSTOM_API_BASE)."
            )
        # Prefer NIKA_CUSTOM_API_KEY; fall back to CUSTOM_API_KEY with warning.
        api_key = resolve_custom_api_key() or None
        # LangChain OpenAI client requires a non-empty key string; unauthenticated
        # local endpoints can omit NIKA_CUSTOM_API_KEY — use a placeholder only
        # for the client constructor (not stored as a real credential).
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key or "no-key",
            temperature=0,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )

    if llm_provider == "anthropic":
        # Optional ANTHROPIC_BASE_URL supports Anthropic-compatible endpoints
        # (e.g. https://api.deepseek.com/anthropic).
        return ChatAnthropic(
            model=model,
            api_key=os.getenv(ENV_ANTHROPIC_API_KEY) or None,
            base_url=os.getenv(ENV_ANTHROPIC_BASE_URL) or None,
            default_request_timeout=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )

    raise ValueError(f"Unsupported llm provider: {llm_provider}")
