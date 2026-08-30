"""Per-session message logger for agent conversations.

Writes every LLM and tool event as a JSON line to::

    {session_dir}/messages.jsonl

Each entry includes an ``agent`` field that identifies which agent produced
the event, enabling the two phases (diagnosis / submission) to share a single
file while remaining easily filterable.

Extending
---------
Add new event types by calling ``log(event_type, payload)`` directly.
Additional top-level fields can be included in ``payload``; they pass through
unchanged to the JSONL record.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs.generation import Generation

from agent.utils.usage import normalize_usage

MESSAGES_FILENAME = "messages.jsonl"


class MessageLogger:
    """Writes structured JSONL message events for one agent phase.

    Parameters
    ----------
    agent:
        Name tag written to every entry (e.g. :data:`~agent.protocols.DIAGNOSIS`).
    session_dir:
        Path to the session results directory (must already exist or be
        creatable).
    """

    def __init__(self, agent: str, session_dir: str) -> None:
        self.agent = agent
        self._path = Path(session_dir) / MESSAGES_FILENAME
        os.makedirs(session_dir, exist_ok=True)

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent": self.agent,
            "event": event_type,
            **payload,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


class AgentCallbackLogger(BaseCallbackHandler):
    """LangChain callback handler that delegates to ``MessageLogger``."""

    def __init__(self, agent: str, session_dir: str) -> None:
        super().__init__()
        self._logger = MessageLogger(agent=agent, session_dir=session_dir)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        **kwargs,
    ) -> None:
        self._logger.log(
            "llm_start",
            {
                "messages": messages[0][-1],
                "model": serialized,
            },
        )

    def on_llm_end(self, response, **kwargs) -> None:
        payload: dict[str, Any] = {}
        try:
            res: Generation = response.generations[0][0]
            if res:
                text = getattr(res, "text", None)
                if text:
                    payload["text"] = res.text
                generation_info = getattr(res, "generation_info", None)
                if generation_info:
                    payload["generation_info"] = res.generation_info
                message = getattr(res, "message", None)
                if message:
                    payload["invalid_tool_calls"] = getattr(
                        message, "invalid_tool_calls", None
                    )
                    raw_usage = getattr(message, "usage_metadata", None)
                    payload["usage_metadata"] = (
                        normalize_usage(raw_usage) if raw_usage else None
                    )
            self._logger.log("llm_end", payload)
        except Exception as exc:
            import traceback

            self._logger.log(
                "llm_end_error",
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "response": str(response),
                },
            )

    def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, **kwargs
    ) -> None:
        self._logger.log("tool_start", {"tool": serialized, "input": input_str})

    def on_tool_end(self, output: ToolMessage, **kwargs) -> None:
        if output.status == "error":
            self._logger.log("tool_error", {"output": output})
            return
        self._logger.log(
            "tool_end", {"output": output, "output_type": type(output).__name__}
        )

    def on_tool_error(self, error, **kwargs) -> None:
        self._logger.log("tool_error", {"error": str(error)})
