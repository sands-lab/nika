"""Per-session message logger for agent conversations.

Writes every LLM and tool event as a JSON line to::

    {session_dir}/messages.jsonl

Both diagnosis and submission pipeline phases are persisted. The final
submission is also represented by ``submission.json``; system events live in
``nika.jsonl``.

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


MESSAGES_FILENAME = "messages.jsonl"


def normalize_tool_input(value: Any) -> str:
    """Serialize tool arguments for stable ``messages.jsonl`` correlation."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def tool_event_payload(
    *,
    name: str | None = None,
    input: Any = None,
    tool_call_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a normalized tool event payload for ``messages.jsonl``."""
    payload: dict[str, Any] = dict(fields)
    if name:
        payload["tool"] = {"name": name}
    if input is not None:
        payload["input"] = normalize_tool_input(input)
    if tool_call_id:
        payload["tool_call_id"] = str(tool_call_id)
    return payload


class PendingToolCallTracker:
    """Correlate tool_start and tool_end when IDs are missing or only on one side."""

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, str]] = {}
        self._queues: dict[str, list[dict[str, str]]] = {}

    def register(
        self,
        *,
        name: str,
        input: Any = None,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        input_str = normalize_tool_input(input) if input is not None else ""
        if tool_call_id:
            self._by_id[str(tool_call_id)] = {"name": name, "input": input_str}
        else:
            self._queues.setdefault(name, []).append({"name": name, "input": input_str})
        return tool_event_payload(name=name, input=input, tool_call_id=tool_call_id)

    def resolve(
        self,
        *,
        name: str | None = None,
        tool_call_id: str | None = None,
        input: Any = None,
    ) -> dict[str, str]:
        if tool_call_id and str(tool_call_id) in self._by_id:
            return self._by_id.pop(str(tool_call_id))
        if name and self._queues.get(name):
            return self._queues[name].pop(0)
        if input is not None:
            return {"name": name or "", "input": normalize_tool_input(input)}
        return {"name": name or "", "input": ""}


def _resolve_tool_name(output: Any, kwargs: dict[str, Any]) -> str | None:
    name = kwargs.get("name")
    if name:
        return str(name)
    tool_name = getattr(output, "name", None)
    if tool_name:
        return str(tool_name)
    return None


class MessageLogger:
    """Writes structured JSONL message events for one agent phase.

    Parameters
    ----------
    phase:
        Name tag written to every entry (e.g. :data:`~agent.protocols.DIAGNOSIS`).
    session_dir:
        Path to the session results directory (must already exist or be
        creatable).
    """

    def __init__(self, phase: str, session_dir: str) -> None:
        self.phase = phase
        self._path = Path(session_dir) / MESSAGES_FILENAME
        os.makedirs(session_dir, exist_ok=True)

    def log(self, event_type: str, payload: dict[str, Any]) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "phase": self.phase,
            "event": event_type,
            **payload,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


class AgentCallbackLogger(BaseCallbackHandler):
    """LangChain callback handler that delegates to ``MessageLogger``."""

    def __init__(self, phase: str, session_dir: str) -> None:
        super().__init__()
        self._logger = MessageLogger(phase=phase, session_dir=session_dir)
        self._pending_tool_calls = PendingToolCallTracker()

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
        tool_name = str(serialized.get("name", ""))
        payload = self._pending_tool_calls.register(
            name=tool_name,
            input=input_str,
            tool_call_id=kwargs.get("tool_call_id"),
        )
        self._logger.log("tool_start", payload)

    def on_tool_end(self, output: ToolMessage, **kwargs) -> None:
        tool_name = _resolve_tool_name(output, kwargs)
        tool_call_id = kwargs.get("tool_call_id") or getattr(
            output, "tool_call_id", None
        )
        resolved = self._pending_tool_calls.resolve(
            name=tool_name,
            tool_call_id=tool_call_id,
            input=kwargs.get("inputs"),
        )
        correlation = tool_event_payload(
            name=tool_name or resolved.get("name") or None,
            input=resolved.get("input") or kwargs.get("inputs"),
            tool_call_id=tool_call_id,
        )
        if getattr(output, "status", None) == "error":
            self._logger.log("tool_error", {**correlation, "output": output})
            return
        self._logger.log(
            "tool_end",
            {
                **correlation,
                "output": output,
                "output_type": type(output).__name__,
            },
        )

    def on_tool_error(self, error, **kwargs) -> None:
        tool_name = _resolve_tool_name(error, kwargs)
        tool_call_id = kwargs.get("tool_call_id")
        resolved = self._pending_tool_calls.resolve(
            name=tool_name,
            tool_call_id=tool_call_id,
            input=kwargs.get("inputs"),
        )
        payload = tool_event_payload(
            name=tool_name or resolved.get("name") or None,
            input=resolved.get("input") or kwargs.get("inputs"),
            tool_call_id=tool_call_id,
            error=str(error),
        )
        self._logger.log("tool_error", payload)
