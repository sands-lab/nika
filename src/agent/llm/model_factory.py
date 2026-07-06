import os

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

load_dotenv()

# A request with no client-side timeout can block an agent step forever on a
# half-dead connection. Override per deployment via env.
LLM_TIMEOUT_SECONDS = float(os.getenv("NIKA_LLM_TIMEOUT", "300"))
LLM_MAX_RETRIES = int(os.getenv("NIKA_LLM_RETRIES", "2"))


def load_model(
    llm_provider: str = "openai", model: str = "gpt-5-mini"
) -> BaseChatModel:
    if llm_provider == "ollama":
        return ChatOllama(
            model=model,
            temperature=0,
            validate_model_on_init=True,
            base_url=os.getenv("OLLAMA_API_URL"),
        )

    if llm_provider == "openai":
        return ChatOpenAI(
            model_name=model,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )

    if llm_provider == "deepseek":
        return ChatDeepSeek(
            model=model,
            base_url="https://api.deepseek.com",
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )

    if llm_provider == "custom":
        return ChatOpenAI(
            model=model,
            base_url=os.getenv("CUSTOM_API_BASE"),
            api_key=os.getenv("CUSTOM_API_KEY", "dummy"),
            temperature=0,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=LLM_MAX_RETRIES,
        )

    raise ValueError(f"Unsupported llm provider: {llm_provider}")
