"""Per-session message logger for agent conversations.

Writes every LLM and tool event as a JSON line to::

    {session_dir}/messages.jsonl

Both diagnosis and submission pipeline phases are persisted. The final
submission is also represented by ``submission.json``; system events live in
``nika.jsonl``.

Lifecycle event contract
------------------------
Two layers (keep names stable so inspect can match uniformly):

* **Session** (``nika.jsonl``, written by NIKA workflows):
  ``agent_start`` → ``agent_end`` | ``agent_error``
  Covers the whole agent process for one trial.

* **Phase** (``messages.jsonl``, written by every agent implementation):
  ``agent_start`` → ``agent_done`` | ``agent_error``
  One pair per pipeline phase (diagnosis / submission).

Every agent emits the phase bookends through
:class:`~agent.utils.two_phase.TwoPhaseAgent` (which calls
:meth:`MessageLogger.log_agent_start` / :meth:`MessageLogger.log_agent_done` /
:meth:`MessageLogger.log_agent_error`), so inspect pairing stays agent-agnostic.
Workers log one ``llm_end`` per model response (the ``max_steps`` unit).

Extending
---------
Add new event types by calling ``log(event_type, payload)`` directly.
Additional top-level fields can be included in ``payload``; they pass through
unchanged to the JSONL record.
"""

import json
import os
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import BaseMessage, ToolMessage
from langchain_core.outputs.generation import Generation

from agent.utils.reasoning_capture import reasoning_fields_for_log
from agent.utils.usage import normalize_usage

MESSAGES_FILENAME = "messages.jsonl"

# Active LangChain chat-model call so HTTP-level retries can emit ``llm_retry``
# markers into the same messages.jsonl span (inspect splits them for display).
_ActiveLlmLog = tuple["MessageLogger", str | None]
_active_llm_log: ContextVar[_ActiveLlmLog | None] = ContextVar(
    "nika_active_llm_log", default=None
)


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


def tool_output_content(output: Any) -> Any:
    """Tool result body for ``messages.jsonl`` (not the ``ToolMessage`` repr).

    Text-only content blocks are joined into one string; other content is kept
    as-is for JSON serialization.
    """
    content = getattr(output, "content", output)
    if isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in content
    ):
        return "\n".join(str(block.get("text", "")) for block in content)
    return content


def _run_id_field(kwargs: dict[str, Any]) -> dict[str, str]:
    """LangChain ``run_id`` so readers can pair concurrent llm_start/llm_end."""
    run_id = kwargs.get("run_id")
    return {"run_id": str(run_id)} if run_id is not None else {}


def log_llm_retry(
    error: BaseException,
    *,
    failed_attempt: int,
    max_retries: int,
) -> None:
    """Record an HTTP/provider retry inside the current ``llm_start`` span.

    OpenAI-compat clients otherwise retry silently between ``llm_start`` and
    ``llm_end``, so inspect would show one multi-timeout wall-clock bar.
    """
    active = _active_llm_log.get()
    if active is None:
        return
    logger, run_id = active
    payload: dict[str, Any] = {
        "error": str(error),
        "failed_attempt": int(failed_attempt),
        "next_attempt": int(failed_attempt) + 1,
        "max_retries": int(max_retries),
    }
    if run_id:
        payload["run_id"] = run_id
    logger.log("llm_retry", payload)


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
            "timestamp": datetime.now(UTC).isoformat(),
            "phase": self.phase,
            "event": event_type,
            **payload,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def log_agent_start(self, **fields: Any) -> None:
        """Phase bookend: phase work is beginning."""
        self.log("agent_start", fields)

    def log_agent_done(self, **fields: Any) -> None:
        """Phase bookend: phase work finished successfully."""
        self.log("agent_done", fields)

    def log_agent_error(self, error: Any, **fields: Any) -> None:
        """Phase bookend: phase work failed."""
        self.log("agent_error", {"error": str(error), **fields})


class AgentCallbackLogger(BaseCallbackHandler):
    """LangChain callback handler that delegates to ``MessageLogger``."""

    def __init__(self, phase: str, session_dir: str) -> None:
        super().__init__()
        self._logger = MessageLogger(phase=phase, session_dir=session_dir)
        self._pending_tool_calls = PendingToolCallTracker()
        self._active_llm_tokens: list[Token] = []
        # Latest non-empty model text; the report when max_steps runs out.
        self.last_text = ""

    def _push_active_llm(self, run_id: str | None) -> None:
        token = _active_llm_log.set((self._logger, run_id))
        self._active_llm_tokens.append(token)

    def _pop_active_llm(self) -> None:
        if not self._active_llm_tokens:
            return
        token = self._active_llm_tokens.pop()
        try:
            _active_llm_log.reset(token)
        except ValueError:
            # LangGraph/LangChain may run on_llm_end in a different Context
            # than on_chat_model_start; Token.reset is context-bound.
            _active_llm_log.set(None)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        **kwargs,
    ) -> None:
        run_fields = _run_id_field(kwargs)
        self._logger.log(
            "llm_start",
            {
                "messages": messages[0][-1],
                # Non-serializable models (e.g. ChatDeepSeek) carry a ``repr``
                # with client fields such as ``openai_api_key=SecretStr(...)``,
                # which fails the leaderboard trajectory secret scan.
                "model": {k: v for k, v in serialized.items() if k != "repr"},
                **run_fields,
            },
        )
        self._push_active_llm(run_fields.get("run_id"))

    def on_llm_end(self, response, **kwargs) -> None:
        payload: dict[str, Any] = _run_id_field(kwargs)
        try:
            res: Generation = response.generations[0][0]
            if res:
                text = getattr(res, "text", None)
                if text:
                    payload["text"] = res.text
                    if str(text).strip():
                        self.last_text = str(text)
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
                    payload.update(reasoning_fields_for_log(message))
            self._logger.log("llm_end", payload)
        except Exception as exc:
            import traceback

            self._logger.log(
                "llm_end_error",
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                    "response": str(response),
                    **_run_id_field(kwargs),
                },
            )
        finally:
            self._pop_active_llm()

    def on_llm_error(self, error: BaseException, **kwargs) -> None:
        # Close the llm_start span for failed calls (timeouts, provider errors).
        try:
            self._logger.log(
                "llm_end_error", {"error": str(error), **_run_id_field(kwargs)}
            )
        finally:
            self._pop_active_llm()

    def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, **kwargs
    ) -> None:
        tool_name = str(serialized.get("name", ""))
        # ``input_str`` is ``str(dict)``; prefer the structured args when given.
        inputs = kwargs.get("inputs")
        payload = self._pending_tool_calls.register(
            name=tool_name,
            input=inputs if isinstance(inputs, dict) else input_str,
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
            self._logger.log(
                "tool_error", {**correlation, "output": tool_output_content(output)}
            )
            return
        self._logger.log(
            "tool_end",
            {
                **correlation,
                "output": tool_output_content(output),
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
