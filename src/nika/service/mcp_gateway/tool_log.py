"""Host-side records a benchmark engine's gateway keeps for the frontend.

A trial gateway that a benchmark engine opens (see
:mod:`nika.service.mcp_gateway.leases`) records each MCP tool call in
``tool_calls.jsonl`` and freezes the diagnosis report in ``messages.jsonl``
when the frontend advances the phase. The records only observe: requests and
responses pass to and from the agent unchanged.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.protocols import DIAGNOSIS
from nika.evaluator.result_log import MESSAGES_FILENAME

TOOL_CALLS_FILENAME = "tool_calls.jsonl"
DIAGNOSIS_FROZEN_EVENT = "diagnosis_frozen"

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _jsonrpc_payload(body: bytes, content_type: str) -> dict[str, Any] | None:
    """The JSON-RPC response in a JSON or server-sent event body."""
    text = body.decode("utf-8", errors="replace")
    candidates = (
        # Only CR and LF end SSE lines; JSON text may hold U+2028 and U+2029.
        [
            line[5:].strip()
            for line in re.split(r"\r\n|\r|\n", text)
            if line.startswith("data:")
        ]
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
    denied: str | None = None,
    call_id: Any = None,
    elapsed_ms: float | None = None,
) -> None:
    """Append one tool call to the session's ``tool_calls.jsonl``.

    Release 0.1.0 does not limit tool output, so ``output`` and ``raw_output``
    are the same text.
    """
    entry = {
        "timestamp": _now_iso(),
        "call_id": call_id,
        "duration_ms": elapsed_ms,
        "raw_output": output,
        "phase": phase,
        "server": server,
        "tool": tool,
        "arguments": arguments,
        "is_error": is_error,
        "denied": denied,
        "output": output,
    }
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    path = Path(session_dir) / TOOL_CALLS_FILENAME
    with _lock, path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def frozen_diagnosis_report(session_dir: str) -> str | None:
    """The report frozen in the session's trajectory, or ``None``."""
    path = Path(session_dir) / MESSAGES_FILENAME
    if not path.is_file():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == DIAGNOSIS_FROZEN_EVENT:
            report = event.get("report")
            return report if isinstance(report, str) else ""
    return None


def freeze_diagnosis(session_dir: str, report: str) -> str:
    """Append the final diagnosis report to the trajectory once; return it."""
    with _lock:
        existing = frozen_diagnosis_report(session_dir)
        if existing is not None:
            return existing
        path = Path(session_dir) / MESSAGES_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": _now_iso(),
                        "phase": DIAGNOSIS,
                        "event": DIAGNOSIS_FROZEN_EVENT,
                        "report": report,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return report
