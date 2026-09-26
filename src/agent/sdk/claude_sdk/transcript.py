"""Map claude-agent-sdk messages to ``messages.jsonl`` events.

Shared by ``sdk.claude_sdk`` and ``community.sade``. One model response
becomes one ``llm_start`` / ``llm_end`` pair (the SDK splits a response into
several ``AssistantMessage`` objects that share a ``message_id``); its tool
calls are logged inside that pair. Per-message SDK usage is a streamed
partial, so the query's authoritative ``ResultMessage`` usage is attached to
the last ``llm_end`` instead, which is why each ``llm_end`` is written only
when the next response starts or the result arrives.
"""

from __future__ import annotations

from typing import Any

from agent.utils.loggers import MessageLogger, tool_event_payload
from agent.utils.two_phase import ERROR_PREFIX, max_steps_report
from agent.utils.usage import normalize_usage


def normalize_tool_name(name: str) -> str:
    """Map claude-agent-sdk MCP names (``mcp__server__tool``) to short tool ids."""
    prefix = "mcp__"
    if name.startswith(prefix):
        remainder = name[len(prefix) :]
        if "__" in remainder:
            return remainder.split("__", 1)[1]
    return name


class ClaudeSdkTranscript:
    """Log one phase's SDK message stream and track LLM turns."""

    def __init__(self, logger: MessageLogger, *, model: str) -> None:
        self._logger = logger
        self.model = model
        self.turns = 0
        self.last_text = ""
        self._message_id: str | None = None
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._open = False
        self._tools: dict[str, tuple[str, str]] = {}

    def _flush(self, usage: Any | None = None) -> None:
        if not self._open:
            return
        payload: dict[str, Any] = {
            "text": "\n".join(self._text),
            "usage_metadata": normalize_usage(usage) if usage is not None else {},
        }
        if self._thinking:
            payload["reasoning_content"] = "\n\n".join(self._thinking)
        self._logger.log("llm_end", payload)
        self._open = False
        self._message_id = None
        self._text = []
        self._thinking = []

    def _begin_response(self, message_id: str | None) -> None:
        if self._open and message_id is not None and message_id == self._message_id:
            return
        self._flush()
        self.turns += 1
        self._open = True
        self._message_id = message_id
        if self.turns > 1:
            # run_claude_sdk_query logs the first response's llm_start.
            self._logger.log("llm_start", {"model": {"name": self.model}})

    def handle(self, message: Any) -> None:
        """Log *message*; call for every message of the phase."""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        if isinstance(message, AssistantMessage):
            self._begin_response(getattr(message, "message_id", None))
            for block in message.content:
                if isinstance(block, ThinkingBlock):
                    self._thinking.append(block.thinking)
                elif isinstance(block, TextBlock):
                    self._text.append(block.text)
                    if block.text.strip():
                        self.last_text = block.text
                elif isinstance(block, ToolUseBlock):
                    name = normalize_tool_name(block.name)
                    self._tools[block.id] = (name, str(block.input))
                    self._logger.log(
                        "tool_start",
                        tool_event_payload(
                            name=name, input=block.input, tool_call_id=block.id
                        ),
                    )
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if not isinstance(block, ToolResultBlock):
                    continue
                name, tool_input = self._tools.get(block.tool_use_id, (None, None))
                correlation = tool_event_payload(
                    name=name, input=tool_input, tool_call_id=block.tool_use_id
                )
                if block.is_error:
                    self._logger.log(
                        "tool_error", {**correlation, "output": str(block.content)}
                    )
                else:
                    self._logger.log(
                        "tool_end",
                        {
                            **correlation,
                            "output": str(block.content),
                            "output_type": "tool_result",
                        },
                    )
        elif isinstance(message, ResultMessage):
            if self._open:
                self._flush(message.usage)
            else:
                # No response left to carry the usage; keep it out of step counts.
                self._logger.log(
                    "sdk_result", {"usage_metadata": normalize_usage(message.usage)}
                )


def result_report(
    message: Any,
    transcript: ClaudeSdkTranscript,
    *,
    max_steps: int,
    logger: MessageLogger,
) -> str:
    """Phase output for a ``ResultMessage`` under the shared max-steps policy."""
    subtype = getattr(message, "subtype", "")
    if subtype == "error_max_turns":
        return max_steps_report(
            logger, max_steps=max_steps, latest_text=transcript.last_text
        )
    if getattr(message, "is_error", False):
        return f"{ERROR_PREFIX} {message.result or subtype}"
    return message.result or transcript.last_text


async def run_claude_sdk_query(
    options: Any,
    prompt: str,
    *,
    logger: MessageLogger,
    transcript: ClaudeSdkTranscript,
    max_steps: int,
) -> str:
    """Run one ``ClaudeSDKClient`` query and return the phase output."""
    from claude_agent_sdk import ClaudeSDKClient, ResultMessage

    logger.log(
        "llm_start",
        {
            "messages": {"role": "user", "content": prompt[:500]},
            "model": {"name": transcript.model},
            "mcp_servers": list((options.mcp_servers or {}).keys()),
        },
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            transcript.handle(message)
            if isinstance(message, ResultMessage):
                return result_report(
                    message, transcript, max_steps=max_steps, logger=logger
                )
    return f"{ERROR_PREFIX} {logger.phase} phase ended without a result"
