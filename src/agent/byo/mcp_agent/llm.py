"""Augmented LLMs with NIKA messages.jsonl logging."""

from __future__ import annotations

import os

from mcp.types import CallToolRequest, CallToolResult
from mcp_agent.workflows.llm.augmented_llm import RequestParams
from mcp_agent.workflows.llm.augmented_llm_anthropic import AnthropicAugmentedLLM
from mcp_agent.workflows.llm.augmented_llm_openai import OpenAIAugmentedLLM

from agent.utils.loggers import MessageLogger
from agent.utils.usage import normalize_usage
from nika.service.mcp_server.registry import MCP_SERVER_PREFIXES

_SERVER_PREFIXES = MCP_SERVER_PREFIXES + ("task_mcp_server_",)


def _short_tool_name(name: str) -> str:
    """Strip mcp-agent server namespace prefix for messages.jsonl parity."""
    for prefix in _SERVER_PREFIXES:
        if name.startswith(prefix):
            return name.removeprefix(prefix)
    return name


def _resolve_provider(provider: str | None = None) -> str:
    if provider and provider.strip():
        return provider.strip().lower()
    return (os.environ.get("NIKA_LLM_PROVIDER") or "openai").strip().lower()


def create_nika_augmented_llm(
    *,
    agent,
    nika_logger: MessageLogger | None,
    default_request_params: RequestParams,
    provider: str | None = None,
):
    """Return the OpenAI or Anthropic AugmentedLLM for the active provider."""
    if _resolve_provider(provider) == "anthropic":
        return NikaAnthropicAugmentedLLM(
            agent=agent,
            nika_logger=nika_logger,
            default_request_params=default_request_params,
        )
    return NikaOpenAIAugmentedLLM(
        agent=agent,
        nika_logger=nika_logger,
        default_request_params=default_request_params,
    )


class _NikaToolLoggingMixin:
    """Shared MCP tool / max-iteration logging for NIKA message logs."""

    _nika_logger: MessageLogger | None
    _max_iterations_reached: bool

    def __init__(
        self, *args, nika_logger: MessageLogger | None = None, **kwargs
    ) -> None:
        super().__init__(*args, **kwargs)
        self._nika_logger = nika_logger
        self._max_iterations_reached = False

    @property
    def max_iterations_reached(self) -> bool:
        return self._max_iterations_reached

    async def pre_tool_call(
        self, tool_call_id: str | None, request: CallToolRequest
    ) -> CallToolRequest | bool:
        if self._nika_logger is not None:
            self._nika_logger.log(
                "tool_start",
                {
                    "tool": {"name": _short_tool_name(request.params.name)},
                    "input": str(request.params.arguments),
                },
            )
        return await super().pre_tool_call(tool_call_id=tool_call_id, request=request)

    async def post_tool_call(
        self,
        tool_call_id: str | None,
        request: CallToolRequest,
        result: CallToolResult,
    ) -> CallToolResult:
        if self._nika_logger is not None:
            if result.isError:
                self._nika_logger.log("tool_error", {"error": str(result.content)})
            else:
                self._nika_logger.log(
                    "tool_end",
                    {"output": str(result.content), "output_type": "CallToolResult"},
                )
        return await super().post_tool_call(
            tool_call_id=tool_call_id, request=request, result=result
        )


class NikaOpenAIAugmentedLLM(_NikaToolLoggingMixin, OpenAIAugmentedLLM):
    """OpenAIAugmentedLLM that writes tool events to ``messages.jsonl``."""

    async def generate_str(self, message, request_params: RequestParams | None = None):
        self._max_iterations_reached = False
        params = self.get_request_params(request_params)
        responses = await self.generate(message=message, request_params=request_params)

        if responses:
            last = responses[-1]
            tool_calls = getattr(last, "tool_calls", None)
            if tool_calls and len(responses) >= params.max_iterations:
                self._max_iterations_reached = True

        final_text: list[str] = []
        for response in responses:
            content = response.content
            if not content:
                continue
            if isinstance(content, str):
                final_text.append(content)
        return "\n".join(final_text)

    def _annotate_span_for_completion_response(self, span, response, turn):
        """Log one ``llm_end`` per ChatCompletion, then keep mcp-agent tracing."""
        if self._nika_logger is not None:
            choices = getattr(response, "choices", None) or []
            message = getattr(choices[0], "message", None) if choices else None
            self._nika_logger.log(
                "llm_end",
                {
                    "text": getattr(message, "content", None) or "",
                    "usage_metadata": normalize_usage(getattr(response, "usage", None)),
                },
            )
        return super()._annotate_span_for_completion_response(span, response, turn)


class NikaAnthropicAugmentedLLM(_NikaToolLoggingMixin, AnthropicAugmentedLLM):
    """AnthropicAugmentedLLM that writes tool events to ``messages.jsonl``."""

    async def generate_str(self, message, request_params: RequestParams | None = None):
        self._max_iterations_reached = False
        params = self.get_request_params(request_params)
        responses = await self.generate(message=message, request_params=request_params)

        if responses:
            last = responses[-1]
            stop = getattr(last, "stop_reason", None)
            if stop == "tool_use" and len(responses) >= params.max_iterations:
                self._max_iterations_reached = True

        final_text: list[str] = []
        for response in responses:
            for block in getattr(response, "content", None) or []:
                if getattr(block, "type", None) == "text" and getattr(
                    block, "text", None
                ):
                    final_text.append(block.text)
        return "\n".join(final_text)

    def _annotate_span_for_completion_response(self, span, response, turn):
        """Log one ``llm_end`` per Anthropic Message, then keep mcp-agent tracing."""
        if self._nika_logger is not None:
            texts: list[str] = []
            for block in getattr(response, "content", None) or []:
                if getattr(block, "type", None) == "text" and getattr(
                    block, "text", None
                ):
                    texts.append(block.text)
            self._nika_logger.log(
                "llm_end",
                {
                    "text": "\n".join(texts),
                    "usage_metadata": normalize_usage(getattr(response, "usage", None)),
                },
            )
        return super()._annotate_span_for_completion_response(span, response, turn)
