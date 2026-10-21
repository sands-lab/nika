"""Host-side record of an agent's MCP tool calls (``tool_calls.jsonl``).

The gateway sees every tool call of a session, so it writes each call, its
arguments, and the result the agent received into the session directory. The
agent cannot reach this file; with the model proxy's ``model_calls.jsonl`` it
is the authoritative record of a run (see :mod:`nika.evaluator.trace_parser`).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TOOL_CALLS_FILENAME = "tool_calls.jsonl"

_lock = threading.Lock()


def _jsonrpc_payload(body: bytes, content_type: str) -> dict[str, Any] | None:
    """The JSON-RPC response in a JSON or server-sent event body."""
    text = body.decode("utf-8", errors="replace")
    candidates = (
        [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        if "event-stream" in content_type
        else [text]
    )
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict) and ("result" in value or "error" in value):
            return value
    return None


def tool_result(body: bytes, content_type: str) -> tuple[bool, str]:
    """``(is_error, text)`` of a ``tools/call`` response body."""
    payload = _jsonrpc_payload(body, content_type)
    if payload is None:
        return True, body.decode("utf-8", errors="replace")
    if "error" in payload:
        error = payload["error"] or {}
        return True, str(error.get("message") if isinstance(error, dict) else error)
    result = payload.get("result") or {}
    text = "\n".join(
        str(block.get("text", ""))
        for block in result.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )
    return bool(result.get("isError")), text


def record_tool_call(
    session_dir: str,
    *,
    phase: str,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    is_error: bool,
    output: str,
    denied: str = "",
) -> None:
    """Append one tool call to the session's ``tool_calls.jsonl``."""
    if not session_dir:
        return
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "phase": phase,
        "server": server,
        "tool": tool,
        "arguments": arguments,
        "is_error": is_error,
        "denied": denied or None,
        "output": output,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    path = Path(session_dir) / TOOL_CALLS_FILENAME
    with _lock, path.open("a", encoding="utf-8") as handle:
        handle.write(line)
