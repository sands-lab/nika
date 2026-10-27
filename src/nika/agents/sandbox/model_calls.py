"""Host-side record of an agent's model calls, and its LLM request budget.

Every model request of a sandboxed agent passes the host model proxy
(:mod:`nika.agents.sandbox.model_proxy`). :class:`ModelCallLog` writes each
exchange to ``model_calls.jsonl`` in the session directory: the request body
exactly as the agent sent it and the response exactly as the agent received it
(the raw server-sent event stream for streamed responses). The sandbox cannot
reach this file, so it is the authoritative record of the run.

The log also enforces ``max_steps`` as a number of LLM requests per phase. Some
harnesses spawn sub-agents whose model calls their own turn limits do not
count; the proxy counts every generation request. Requests the provider could
not serve (rate limits, server errors, unreachable endpoint) are not charged.
The request past the budget is refused, and the host stops the phase (see
:meth:`nika.agents.sandbox.manager.SandboxManager._run_step`).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.protocols import DIAGNOSIS
from agent.utils.usage import normalize_usage

MODEL_CALLS_FILENAME = "model_calls.jsonl"

# Model generation endpoints of the OpenAI and Anthropic APIs. Token counting
# and model listing are not LLM requests.
_GENERATION_SUFFIXES = ("/messages", "/chat/completions", "/responses", "/completions")


def is_generation_request(method: str, path: str) -> bool:
    return method.upper() == "POST" and path.rstrip("/").endswith(_GENERATION_SUFFIXES)


def provider_failure(status: int) -> bool:
    """Whether *status* means the provider could not serve the request."""
    return status == 429 or status >= 500


def _uses_tools(request: Any) -> bool:
    """Whether a request body offers tools, as an agent's working loop does.

    Harness side requests (titles, topic checks, summaries) offer none.
    """
    return isinstance(request, dict) and bool(request.get("tools"))


def _json_or_text(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _sse_events(text: str) -> list[dict[str, Any]]:
    """JSON payloads of the ``data:`` lines of a server-sent event stream."""
    events = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            value = json.loads(payload)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _message_text(body: dict[str, Any]) -> str:
    """Assistant text of one non-streamed Chat, Messages, or Responses reply."""
    if isinstance(body.get("choices"), list):
        parts = []
        for choice in body["choices"]:
            content = (choice.get("message") or {}).get("content")
            if isinstance(content, str):
                parts.append(content)
        return "".join(parts)
    if body.get("type") == "message" and isinstance(body.get("content"), list):
        return "".join(
            block.get("text", "")
            for block in body["content"]
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if isinstance(body.get("output"), list):
        return "".join(
            part.get("text", "")
            for item in body["output"]
            if isinstance(item, dict) and item.get("type") == "message"
            for part in item.get("content") or []
            if isinstance(part, dict) and part.get("type") == "output_text"
        )
    return ""


def _stream_text_and_usage(events: list[dict[str, Any]]) -> tuple[str, Any]:
    """Assistant text and usage of a streamed Chat, Messages, or Responses reply."""
    text: list[str] = []
    usage: dict[str, Any] = {}
    for event in events:
        kind = event.get("type")
        if kind == "response.completed":
            # The final Responses event repeats the whole reply.
            response = event.get("response") or {}
            return _message_text(response), response.get("usage")
        if kind == "response.output_text.delta":
            text.append(str(event.get("delta") or ""))
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text.append(str(delta.get("text") or ""))
        elif kind == "message_start":
            usage.update((event.get("message") or {}).get("usage") or {})
        elif kind == "message_delta":
            usage.update(event.get("usage") or {})
        elif isinstance(event.get("choices"), list):
            for choice in event["choices"]:
                content = (choice.get("delta") or {}).get("content")
                if isinstance(content, str):
                    text.append(content)
            if event.get("usage"):
                usage = event["usage"]
    return "".join(text), usage or None


def response_text_and_usage(body: Any, *, streamed: bool) -> tuple[str, Any]:
    """Assistant text and provider usage of a recorded response body.

    String bodies with SSE ``data:`` lines are parsed as streams even when the
    proxy did not mark the reply as streamed.
    """
    if isinstance(body, str):
        events = _sse_events(body)
        if streamed or events:
            return _stream_text_and_usage(events)
    if isinstance(body, dict):
        return _message_text(body), body.get("usage")
    return "", None


@dataclass
class ModelCallLog:
    """Append-only model call record of one session, with a per-phase budget.

    *max_requests* is the LLM request budget of each phase (``None``: no
    budget). The manager moves the log to the next phase with :meth:`set_phase`.
    """

    path: Path
    max_requests: int | None = None
    phase: str = DIAGNOSIS
    exhausted: threading.Event = field(default_factory=threading.Event)
    _counted: int = 0
    _seq: int = 0
    _in_flight: int = 0
    # Latest reply text per phase, from requests that offer tools and from any.
    _last_text: dict[tuple[str, bool], str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _idle: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self._idle = threading.Condition(self._lock)

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase
            self._counted = 0
            self.exhausted.clear()

    def admit(self, method: str, path: str) -> tuple[int, str, bool, bool]:
        """Register a request; return ``(seq, phase, counted, allowed)``.

        A generation request past the phase budget is not allowed, and sets
        :attr:`exhausted`. Every allowed request must be :meth:`record`-ed.
        """
        with self._lock:
            self._seq += 1
            counted = is_generation_request(method, path)
            allowed = True
            if counted:
                if self.max_requests is not None and self._counted >= self.max_requests:
                    allowed = False
                    self.exhausted.set()
                else:
                    self._counted += 1
            if allowed:
                self._in_flight += 1
            return self._seq, self.phase, counted, allowed

    def record(
        self,
        *,
        seq: int,
        phase: str,
        counted: bool,
        method: str,
        path: str,
        request: bytes,
        status: int,
        response: bytes,
        streamed: bool,
        started: float,
        admitted: bool = True,
    ) -> None:
        """Append one exchange; the request and response are kept verbatim.

        *admitted* is ``False`` for a request :meth:`admit` refused.
        """
        body = (
            response.decode("utf-8", errors="replace")
            if streamed
            else (_json_or_text(response))
        )
        text, usage = response_text_and_usage(body, streamed=streamed)
        request_body = _json_or_text(request)
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "seq": seq,
            "phase": phase,
            "llm_request": counted,
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "streamed": streamed,
            "usage": normalize_usage(usage) if counted else None,
            "request": request_body,
            "response": body,
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            if counted and status < 400 and text.strip():
                self._last_text[phase, _uses_tools(request_body)] = text
            if admitted:
                if counted and provider_failure(status) and phase == self.phase:
                    self._counted -= 1
                self._in_flight = max(self._in_flight - 1, 0)
                self._idle.notify_all()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    def last_text(self, phase: str, *, wait_sec: float = 30.0) -> str:
        """Assistant text of the latest successful LLM reply in *phase*.

        Waits up to *wait_sec* for requests still in flight. Prefers replies
        to requests that offer tools: the agent's own loop, not side requests.
        """
        with self._idle:
            self._idle.wait_for(lambda: self._in_flight == 0, timeout=wait_sec)
            return self._last_text.get((phase, True)) or self._last_text.get(
                (phase, False), ""
            )


def model_call_metrics(path: Path, *, phase: str | None = DIAGNOSIS) -> dict[str, int]:
    """``steps`` and token counts of *phase* from ``model_calls.jsonl``."""
    steps = in_tokens = out_tokens = reasoning_tokens = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            entry = json.loads(line)
            if phase is not None and entry.get("phase") != phase:
                continue
            if not entry.get("llm_request") or int(entry.get("status") or 0) >= 400:
                continue
            steps += 1
            usage = entry.get("usage") or {}
            in_tokens += int(usage.get("input_tokens") or 0)
            out_tokens += int(usage.get("output_tokens") or 0)
            reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
    return {
        "steps": steps,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "reasoning_tokens": reasoning_tokens,
    }
