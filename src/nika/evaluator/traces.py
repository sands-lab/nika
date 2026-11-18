"""Events NIKA itself writes into a session's ``messages.jsonl``.

Agents write their own events in the sandbox; the host appends the events of
the steps it runs: budget stops, and the unstructured submission (model calls,
``submission_sample``, ``submission_vote``, ``submission_agreement``, and the
``submit`` tool call).
Rows use the trace format of the runtime contract
(``{timestamp, phase, event, **payload}``), so inspect and the trace parser
read host and agent rows alike.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import BaseMessage

from agent.runtime.contract import MESSAGES_FILENAME
from nika.evaluator.usage import normalize_usage


def message_text(message: Any) -> str:
    """Text content of a model message (text blocks joined by newlines)."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
            if not isinstance(block, dict) or block.get("type") == "text"
        )
    return str(content)


def tool_event_payload(
    *, name: str, input: Any, tool_call_id: str, **fields: Any
) -> dict[str, Any]:
    """Payload of a ``tool_start`` / ``tool_end`` row."""
    return {
        **fields,
        "tool": {"name": name},
        "input": json.dumps(input, ensure_ascii=False, sort_keys=True, default=str),
        "tool_call_id": tool_call_id,
    }


class TraceLogger:
    """Appends host events of one phase to ``{trace_dir}/messages.jsonl``.

    With *echo*, each row's ``message`` field is also printed to stderr.
    """

    def __init__(self, phase: str, trace_dir: str, *, echo: bool = False) -> None:
        self.phase = phase
        self.echo = echo
        self._path = Path(trace_dir) / MESSAGES_FILENAME
        os.makedirs(trace_dir, exist_ok=True)

    def log(self, event: str, payload: dict[str, Any]) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "phase": self.phase,
            "event": event,
            **payload,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        if self.echo and payload.get("message"):
            print(payload["message"], file=sys.stderr, flush=True)

    def log_agent_start(self, **fields: Any) -> None:
        """Phase bookend: phase work is beginning."""
        self.log("agent_start", fields)

    def log_agent_done(self, **fields: Any) -> None:
        """Phase bookend: phase work finished successfully."""
        self.log("agent_done", fields)

    def log_agent_error(self, error: Any, **fields: Any) -> None:
        """Phase bookend: phase work failed."""
        self.log("agent_error", {"error": str(error), **fields})


def _run_id(kwargs: dict[str, Any]) -> dict[str, str]:
    run_id = kwargs.get("run_id")
    return {"run_id": str(run_id)} if run_id is not None else {}


class ModelCallTracer(BaseCallbackHandler):
    """Records host model calls as ``prompt``, ``llm_start`` and ``llm_end`` rows.

    ``prompt`` holds the first call's full input once; ``llm_end`` carries the
    reply text and the normalized ``usage_metadata`` the trace parser counts.
    """

    def __init__(self, logger: TraceLogger) -> None:
        super().__init__()
        self._logger = logger
        self._prompt_logged = False

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        **kwargs: Any,
    ) -> None:
        if not self._prompt_logged:
            self._prompt_logged = True
            self._logger.log("prompt", {"text": _render(messages[0])})
        self._logger.log(
            "llm_start",
            {
                "messages": messages[0][-1],
                # ``repr`` can carry client secrets (``api_key=SecretStr(...)``).
                "model": {k: v for k, v in serialized.items() if k != "repr"},
                **_run_id(kwargs),
            },
        )

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        payload: dict[str, Any] = _run_id(kwargs)
        generation = response.generations[0][0] if response.generations else None
        if generation is not None:
            if generation.text:
                payload["text"] = generation.text
            if generation.generation_info:
                payload["generation_info"] = generation.generation_info
            message = getattr(generation, "message", None)
            if message is not None:
                payload["invalid_tool_calls"] = getattr(
                    message, "invalid_tool_calls", None
                )
                usage = getattr(message, "usage_metadata", None)
                payload["usage_metadata"] = normalize_usage(usage) if usage else None
                reasoning = message.additional_kwargs.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning.strip():
                    payload["reasoning_content"] = reasoning.strip()
        self._logger.log("llm_end", payload)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._logger.log("llm_end_error", {"error": str(error), **_run_id(kwargs)})


def _render(messages: list[BaseMessage]) -> str:
    """Role-labelled plain text of the initial model input."""
    turns = []
    for message in messages:
        content = message.content
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        turns.append(f"[{message.type}]\n{content}")
    return "\n\n".join(turns)
