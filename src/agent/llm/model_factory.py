import os

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_deepseek import ChatDeepSeek
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

load_dotenv()


def load_model(llm_provider: str = "openai", model: str = "gpt-5-mini") -> BaseChatModel:
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
        )

    if llm_provider == "deepseek":
        return ChatDeepSeek(
            model=model,
            base_url="https://api.deepseek.com",
        )

    if llm_provider == "custom":
        return ChatOpenAI(
            model=model,
            base_url=os.getenv("CUSTOM_API_BASE"),
            api_key=os.getenv("CUSTOM_API_KEY", "dummy"),
            temperature=0,
        )

    raise ValueError(f"Unsupported llm provider: {llm_provider}")
