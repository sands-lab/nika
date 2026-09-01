import os

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from agent.utils.provider_env import (
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_BASE_URL,
    resolve_custom_api_key,
    resolve_custom_base_url,
)

load_dotenv()


def _llm_client_settings() -> tuple[float, int]:
    """Return LangGraph client timeout and retry settings."""
    try:
        from nika.run_config.loader import get_run_config

        llm = get_run_config().agent.llm
        return float(llm.timeout_sec), int(llm.max_retries)
    except Exception:  # noqa: BLE001 - sandbox / early import
        return 300.0, 2


def load_model(
    llm_provider: str = "openai",
    model: str = "gpt-5-mini",
    *,
    reasoning_effort: str | None = None,
    timeout_sec: float | None = None,
    max_retries: int | None = None,
) -> BaseChatModel:
    cfg_timeout, cfg_retries = _llm_client_settings()
    timeout = cfg_timeout if timeout_sec is None else float(timeout_sec)
    retries = cfg_retries if max_retries is None else int(max_retries)

    if llm_provider == "openai":
        kwargs: dict = {
            "model_name": model,
            "timeout": timeout,
            "max_retries": retries,
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        base = os.getenv(ENV_OPENAI_BASE_URL) or resolve_custom_base_url() or None
        if base:
            kwargs["base_url"] = base
        return ChatOpenAI(**kwargs)

    if llm_provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek
        return ChatDeepSeek(
            model=model,
            api_key=os.getenv(ENV_DEEPSEEK_API_KEY) or None,
            base_url=DEEPSEEK_OPENAI_BASE_URL,
            timeout=timeout,
            max_retries=retries,
        )

    if llm_provider == "custom":
        base_url = resolve_custom_base_url()
        if not base_url:
            try:
                from nika.run_config.loader import get_run_config

                base_url = (
                    get_run_config().agent.custom.base_url or ""
                ).strip() or None
            except Exception:  # noqa: BLE001
                base_url = None
        if not base_url:
            raise ValueError(
                "Missing agent.custom.base_url: set it in config/nika.yaml "
                "when agent.provider is custom."
            )
        # resolve_custom_api_key warns when it uses the deprecated CUSTOM_API_KEY.
        api_key = resolve_custom_api_key() or None
        # ChatOpenAI requires a non-empty key even for unauthenticated local servers.
        kwargs = {
            "model": model,
            "base_url": base_url,
            "api_key": api_key or "no-key",
            "temperature": 0,
            "timeout": timeout,
            "max_retries": retries,
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        return ChatOpenAI(**kwargs)

    if llm_provider == "anthropic":
        # Official Anthropic needs no base URL. Provider mapping supplies one for gateways.
        from langchain_anthropic import ChatAnthropic
        kwargs = {
            "model": model,
            "api_key": os.getenv(ENV_ANTHROPIC_API_KEY) or None,
            "base_url": os.getenv(ENV_ANTHROPIC_BASE_URL)
            or resolve_custom_base_url()
            or None,
            "default_request_timeout": timeout,
            "max_retries": retries,
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        return ChatAnthropic(**kwargs)

    raise ValueError(f"Unsupported llm provider: {llm_provider}")
