import asyncio
import json
import os
import re
import time
import uuid
from typing import Any

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import (
    BaseChatModel,
    agenerate_from_stream,
    generate_from_stream,
)
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

from agent.utils.loggers import log_llm_retry
from agent.utils.provider_env import (
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_BASE_URL,
    resolve_custom_api_key,
    resolve_custom_base_url,
)
from agent.utils.reasoning_capture import attach_openai_reasoning

load_dotenv()


def _llm_client_settings() -> tuple[float, int, bool]:
    """Return LangGraph client timeout, retry and streaming settings."""
    try:
        from nika.run_config.loader import get_run_config

        llm = get_run_config().agent.llm
        return float(llm.timeout_sec), int(llm.max_retries), bool(llm.stream)
    except Exception:  # noqa: BLE001 - sandbox / early import
        return 480.0, 2, True


def _openai_message_from_choice(choice: Any) -> Any | None:
    if choice is None:
        return None
    if isinstance(choice, dict):
        return choice.get("message") or choice.get("delta")
    return getattr(choice, "message", None) or getattr(choice, "delta", None)


def _retryable_llm_error(exc: BaseException) -> bool:
    """Match OpenAI client retry policy for timeouts / connection / 5xx / 429."""
    try:
        import httpx
        from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError
    except ImportError:  # pragma: no cover - openai always present with ChatOpenAI
        return "Timeout" in type(exc).__name__ or "Connection" in type(exc).__name__

    # A streamed body raises transport errors unwrapped (ReadError, ReadTimeout,
    # RemoteProtocolError) where a buffered response raises APIConnectionError.
    if isinstance(exc, (APITimeoutError, APIConnectionError, httpx.TransportError)):
        return True
    if isinstance(exc, APIStatusError):
        code = int(getattr(exc, "status_code", 0) or 0)
        return code in {408, 409, 429} or code >= 500
    if type(exc) is APIError:
        # Error event inside a stream; retry unless it carries a client error code.
        body = exc.body if isinstance(exc.body, dict) else {}
        code = str(body.get("code") or "")
        return not code.isdigit() or int(code) in {408, 409, 429} or int(code) >= 500
    name = type(exc).__name__
    return "Timeout" in name or "Connection" in name


def _retry_backoff_sec(failed_attempt: int) -> float:
    # Same shape as openai._base_client (0.5 · 2^n, capped).
    return min(0.5 * (2.0 ** max(0, failed_attempt - 1)), 8.0)


# Qwen3-coder style tool-call XML (the format vLLM's qwen3_coder parser reads).
_XML_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>", re.S
)
_XML_PARAM_RE = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.S)
_TRAILING_XML_TOOL_CALLS_RE = re.compile(
    r"(?:<tool_call>\s*<function=[^>\s]+>.*?</function>\s*</tool_call>\s*)+\Z", re.S
)


def _coerce_tool_arg(raw: str, schema: dict[str, Any]) -> Any:
    kind = schema.get("type")
    try:
        if kind == "integer":
            return int(raw.strip())
        if kind == "number":
            return float(raw.strip())
        if kind == "boolean" and raw.strip().lower() in ("true", "false"):
            return raw.strip().lower() == "true"
        if kind in ("object", "array"):
            return json.loads(raw)
    except ValueError:
        pass
    return raw


def _bound_tool_schemas(tools: Any) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools or []:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            params = fn.get("parameters") or {}
            schemas[fn["name"]] = params.get("properties") or {}
    return schemas


def _recover_reasoning_tool_calls(result: ChatResult, tools: Any) -> None:
    """Recover tool calls a Qwen model wrote inside an unclosed think block.

    Qwen3.x on vLLM sometimes emits ``<tool_call>`` without first closing
    ``</think>``. The reasoning parser then files the whole output as reasoning,
    no ``tool_calls`` are returned, and the ReAct loop ends after one round.
    Only trailing, complete calls to bound tools are recovered.
    """
    schemas = _bound_tool_schemas(tools)
    if not schemas:
        return
    for generation in result.generations:
        message = generation.message
        if not isinstance(message, AIMessage) or message.tool_calls:
            continue
        reasoning = message.additional_kwargs.get("reasoning_content")
        if not isinstance(reasoning, str):
            continue
        tail = _TRAILING_XML_TOOL_CALLS_RE.search(reasoning.rstrip())
        if tail is None:
            continue
        found = _XML_TOOL_CALL_RE.findall(tail.group(0))
        if not found or any(name not in schemas for name, _ in found):
            continue
        calls = []
        for name, body in found:
            args = {
                key: _coerce_tool_arg(value, schemas[name].get(key) or {})
                for key, value in _XML_PARAM_RE.findall(body)
            }
            calls.append(
                {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:24]}"}
            )
        if calls:
            message.tool_calls = calls
            if generation.generation_info is not None:
                generation.generation_info["finish_reason"] = "tool_calls"


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI that keeps OpenAI-compat ``reasoning`` / ``reasoning_content``.

    Upstream ``ChatOpenAI`` drops these fields when building ``AIMessage``.
    DeepSeek's client already preserves them; openai/custom providers need the
    same so NIKA can log thinking for Qwen/vLLM and similar servers.

    Tool calls a Qwen model leaves inside its reasoning are recovered (see
    ``_recover_reasoning_tool_calls``).

    HTTP retries are owned here (client ``max_retries=0``) so each failed
    attempt can emit ``llm_retry`` into ``messages.jsonl`` for inspect.

    With ``_nika_stream`` each attempt streams and is aggregated here, so the
    client timeout bounds the gap between chunks instead of the whole response.
    ``streaming=True`` is not used: LangChain would then call ``_stream``
    directly and skip these retries and the tool-call recovery.
    """

    _nika_max_retries: int = PrivateAttr(default=0)
    _nika_stream: bool = PrivateAttr(default=False)

    def _stream_attempt(self, kwargs: dict[str, Any]) -> bool:
        # Structured output (response_format) stays on the parse endpoint.
        return self._nika_stream and "response_format" not in kwargs

    def _create_chat_result(
        self,
        response: dict | Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        if not result.generations:
            return result

        choices: Any
        if isinstance(response, dict):
            choices = response.get("choices") or []
        else:
            choices = getattr(response, "choices", None) or []

        for index, generation in enumerate(result.generations):
            choice = choices[index] if index < len(choices) else None
            attach_openai_reasoning(
                generation.message, _openai_message_from_choice(choice)
            )
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None
        choices = chunk.get("choices") or []
        if not choices:
            return generation_chunk
        top = choices[0]
        delta = (top.get("delta") if isinstance(top, dict) else None) or {}
        # Keep raw deltas: chunks are concatenated on merge, so stripping them
        # (as attach_openai_reasoning does) would drop whitespace between tokens.
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(generation_chunk.message, AIMessageChunk) and isinstance(
            reasoning, str
        ):
            generation_chunk.message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        retries = int(self._nika_max_retries or 0)
        for attempt in range(retries + 1):
            try:
                if self._stream_attempt(kwargs):
                    result = generate_from_stream(
                        self._stream(
                            messages,
                            stop=stop,
                            run_manager=run_manager,
                            **kwargs,
                        )
                    )
                else:
                    result = super()._generate(
                        messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                _recover_reasoning_tool_calls(result, kwargs.get("tools"))
                return result
            except Exception as exc:
                if attempt >= retries or not _retryable_llm_error(exc):
                    raise
                log_llm_retry(exc, failed_attempt=attempt + 1, max_retries=retries)
                time.sleep(_retry_backoff_sec(attempt + 1))
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        retries = int(self._nika_max_retries or 0)
        for attempt in range(retries + 1):
            try:
                if self._stream_attempt(kwargs):
                    result = await agenerate_from_stream(
                        self._astream(
                            messages,
                            stop=stop,
                            run_manager=run_manager,
                            **kwargs,
                        )
                    )
                else:
                    result = await super()._agenerate(
                        messages, stop=stop, run_manager=run_manager, **kwargs
                    )
                _recover_reasoning_tool_calls(result, kwargs.get("tools"))
                return result
            except Exception as exc:
                if attempt >= retries or not _retryable_llm_error(exc):
                    raise
                log_llm_retry(exc, failed_attempt=attempt + 1, max_retries=retries)
                await asyncio.sleep(_retry_backoff_sec(attempt + 1))
        raise RuntimeError("unreachable")  # pragma: no cover


def _openai_model(*, retries: int, stream: bool, **kwargs: Any) -> ReasoningChatOpenAI:
    """Build ReasoningChatOpenAI with client retries off; NIKA owns retry + logs."""
    kwargs["max_retries"] = 0
    if stream:
        # A custom base_url turns streamed usage off by default.
        kwargs["stream_usage"] = True
    model = ReasoningChatOpenAI(**kwargs)
    model._nika_max_retries = int(retries)
    model._nika_stream = bool(stream)
    return model


def _custom_extra_body(reasoning_effort: str | None) -> dict[str, Any] | None:
    """Enable thinking on Qwen/vLLM-style custom servers when effort is set."""
    if reasoning_effort is None or reasoning_effort == "none":
        return None
    return {"chat_template_kwargs": {"enable_thinking": True}}


def load_model(
    llm_provider: str = "openai",
    model: str = "gpt-5-mini",
    *,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    timeout_sec: float | None = None,
    max_retries: int | None = None,
) -> BaseChatModel:
    cfg_timeout, cfg_retries, stream = _llm_client_settings()
    timeout = cfg_timeout if timeout_sec is None else float(timeout_sec)
    retries = cfg_retries if max_retries is None else int(max_retries)

    if llm_provider == "openai":
        kwargs: dict = {
            "model_name": model,
            "timeout": timeout,
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        if max_tokens is not None:
            # Sent as ``max_completion_tokens``, as OpenAI reasoning models require.
            kwargs["max_tokens"] = max_tokens
        base = os.getenv(ENV_OPENAI_BASE_URL) or resolve_custom_base_url() or None
        if base:
            kwargs["base_url"] = base
        return _openai_model(retries=retries, stream=stream, **kwargs)

    if llm_provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        kwargs = {
            "model": model,
            "api_key": os.getenv(ENV_DEEPSEEK_API_KEY) or None,
            "base_url": DEEPSEEK_OPENAI_BASE_URL,
            "timeout": timeout,
            "max_retries": retries,
            "max_tokens": max_tokens,
        }
        if stream:
            # A custom base_url turns streamed usage off by default.
            kwargs.update(streaming=True, stream_usage=True)
        return ChatDeepSeek(**kwargs)

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
        }
        # Do not forward reasoning_effort: many OpenAI-compat servers (vLLM/Qwen)
        # reject NIKA levels like ``xhigh``. Thinking is enabled via extra_body.
        extra_body = _custom_extra_body(reasoning_effort) or {}
        if max_tokens is not None:
            # ChatOpenAI renames max_tokens to max_completion_tokens, which not
            # every OpenAI-compat server accepts.
            extra_body["max_tokens"] = max_tokens
        if extra_body:
            kwargs["extra_body"] = extra_body
        return _openai_model(retries=retries, stream=stream, **kwargs)

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
        if stream:
            kwargs["streaming"] = True
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return ChatAnthropic(**kwargs)

    raise ValueError(f"Unsupported llm provider: {llm_provider}")
