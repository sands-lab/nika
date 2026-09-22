"""Adapt messages.jsonl and nika.jsonl rows into CanonicalTraceEvent."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from nika.inspect.models import CanonicalTraceEvent, EventKind, ToolPayload

_LIFECYCLE_EVENTS = frozenset(
    {
        "env_start",
        "env_verify",
        "env_ready",
        "failure_injected",
        "failure_verified",
        "failure_inject_complete",
        "failure_inject_error",
        "traffic_start",
        "traffic_stop",
        # Session bookends (nika.jsonl): agent_start → agent_end | agent_error
        # Phase bookends (messages.jsonl): agent_start → agent_done | agent_error
        # Sandbox bookends (nika.jsonl): sandbox_start → sandbox_end
        "agent_start",
        "agent_done",
        "agent_end",
        "agent_error",
        "sandbox_start",
        "sandbox_end",
        "eval_metrics_saved",
        "eval_publish",
        "session_close",
        "session_closed",
        "lab_undeploy",
        "lab_undeployed",
        "mcp_attach",
        "mcp_detach",
    }
)

# Codex provider errors often echo the full chat history inside one string.
# Cap before the timeline API / PrettyValue try to render them.
_RAW_STR_LIMIT = 4_000
_RAW_LIST_LIMIT = 40
_RAW_DEPTH_LIMIT = 12


def _truncate(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
    # Avoid whitespace-collapsing multi-MB provider dumps.
    if len(text) > limit * 8:
        text = text[: limit * 8]
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _unwrap_error_message(text: str) -> str:
    """Peel nested JSON ``{error:{message}}`` / ``{message}`` wrappers."""
    cur = text.strip()
    for _ in range(4):
        if len(cur) < 2 or cur[0] != "{":
            break
        try:
            obj = json.loads(cur)
        except json.JSONDecodeError:
            # Provider sometimes stores a Python-dict repr; keep as-is.
            break
        if not isinstance(obj, dict):
            break
        nested = obj.get("error")
        if isinstance(nested, dict) and nested.get("message") is not None:
            cur = str(nested["message"]).strip()
            continue
        if nested is not None and not isinstance(nested, dict):
            cur = str(nested).strip()
            continue
        if obj.get("message") is not None:
            cur = str(obj["message"]).strip()
            continue
        break
    return cur


def _summarize_error_blob(value: Any) -> str:
    """Short ledger line for provider errors that embed full chat transcripts."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _unwrap_error_message(text)
    # Drop echoed request payloads after the validation header / msg field.
    for marker in ("'input':", '"input":', ", 'input':", ', "input":'):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip(" ,:{")
            break
    # Prefer the first non-empty lines (e.g. "197 validation errors:" + msg).
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        head = lines[0]
        if len(lines) > 1 and len(head) < 80:
            head = f"{head} {lines[1]}"
        text = head
    # Collapse pydantic-style dict noise to the human ``msg`` when present.
    for key in ("'msg': ", '"msg": '):
        idx = text.find(key)
        if idx < 0:
            continue
        rest = text[idx + len(key) :]
        quote = rest[:1]
        if quote not in {"'", '"'}:
            continue
        end = rest.find(quote, 1)
        if end > 1:
            msg = rest[1:end]
            prefix = lines[0] if lines and lines[0].endswith(":") else ""
            text = f"{prefix} {msg}".strip() if prefix else msg
            break
    return _truncate(text)


def _codex_error_text(entry: dict[str, Any]) -> str | None:
    """Pull human-readable text from Codex ``error`` / ``turn.failed`` events."""
    if entry.get("message"):
        return str(entry["message"])
    if entry.get("error") is not None:
        err = entry["error"]
        if isinstance(err, dict) and err.get("message") is not None:
            return str(err["message"])
        return str(err)
    codex = entry.get("codex_event")
    if not isinstance(codex, dict):
        return None
    if codex.get("message") is not None:
        return str(codex["message"])
    err = codex.get("error")
    if isinstance(err, dict) and err.get("message") is not None:
        return str(err["message"])
    if err is not None:
        return str(err)
    return None


def _summarize_subprocess_stderr(value: Any) -> str:
    """Prefer the first ERROR / error= line over Codex stdin chatter."""
    if value is None:
        return ""
    text = str(value)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        idx = stripped.find("error=")
        if idx >= 0:
            return _truncate(stripped[idx + len("error=") :].strip())
        lower = stripped.lower()
        if " error " in f" {lower} " or lower.startswith("error"):
            return _truncate(stripped)
    return _truncate(text)

def _slim_raw(value: Any, *, depth: int = 0) -> Any:
    """Truncate oversized strings/lists in event ``raw`` for the timeline API."""
    if depth > _RAW_DEPTH_LIMIT:
        return "…"
    if isinstance(value, str):
        if len(value) <= _RAW_STR_LIMIT:
            return value
        return f"{value[:_RAW_STR_LIMIT]}… [{len(value)} chars total]"
    if isinstance(value, dict):
        return {str(k): _slim_raw(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > _RAW_LIST_LIMIT:
            head = [_slim_raw(v, depth=depth + 1) for v in value[:_RAW_LIST_LIMIT]]
            head.append(f"… [{len(value) - _RAW_LIST_LIMIT} more items]")
            return head
        return [_slim_raw(v, depth=depth + 1) for v in value]
    return value


def _maybe_json(value: Any) -> Any:
    """Parse JSON object/array strings; leave other values unchanged."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) < 2 or text[0] not in "{[" or text[-1] not in "}]":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _codex_item(entry: dict[str, Any]) -> dict[str, Any] | None:
    item = (entry.get("codex_event") or {}).get("item")
    return item if isinstance(item, dict) else None


def _claude_event(entry: dict[str, Any]) -> dict[str, Any] | None:
    claude = entry.get("claude_event")
    return claude if isinstance(claude, dict) else None


def _claude_content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Content blocks from Claude stream-json ``message.content``."""
    claude = _claude_event(entry)
    if not claude:
        return []
    message = claude.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _first_content_block(
    blocks: list[dict[str, Any]], block_type: str
) -> dict[str, Any] | None:
    for block in blocks:
        if block.get("type") == block_type:
            return block
    return None


def _claude_text(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif block_type == "thinking" and block.get("thinking"):
            parts.append(str(block["thinking"]))
    return "\n".join(parts)


def _claude_tool_result_fields(
    entry: dict[str, Any],
) -> tuple[Any, str | None, bool] | None:
    """Parse Claude tool result: ``(output, tool_call_id, is_error)`` or None.

    Handles both structured ``message.content[]`` tool_result blocks and the
    alternate shape where ``message`` is a string and ``tool_use_result`` is set.
    """
    tool_result = _first_content_block(_claude_content_blocks(entry), "tool_result")
    if tool_result:
        call_id = tool_result.get("tool_use_id")
        return (
            tool_result.get("content"),
            str(call_id) if call_id is not None else None,
            bool(tool_result.get("is_error")),
        )

    claude = _claude_event(entry)
    if not claude:
        return None
    if claude.get("type") != "user" and entry.get("event") != "user":
        return None

    result = claude.get("tool_use_result")
    message = claude.get("message")
    if result is not None:
        output: Any = result
    elif isinstance(message, str) and message.strip():
        output = message
    else:
        return None

    call_id = claude.get("tool_use_id")
    if call_id is None:
        parent = claude.get("parent_tool_use_id")
        call_id = parent if parent not in (None, "") else None
    text = str(output)
    is_error = text.lower().startswith("error") or (
        isinstance(message, str) and message.lower().startswith("error")
    )
    return output, str(call_id) if call_id is not None else None, is_error


def _tool_name(entry: dict[str, Any]) -> str | None:
    tool = entry.get("tool")
    if isinstance(tool, dict) and tool.get("name"):
        return str(tool["name"])
    if entry.get("name"):
        return str(entry["name"])
    codex_item = _codex_item(entry)
    if codex_item:
        if codex_item.get("tool"):
            return str(codex_item["tool"])
        if codex_item.get("name"):
            return str(codex_item["name"])
    tool_use = _first_content_block(_claude_content_blocks(entry), "tool_use")
    if tool_use and tool_use.get("name"):
        return str(tool_use["name"])
    return None


def _tool_from_entry(entry: dict[str, Any], *, error: str | None = None) -> ToolPayload:
    tool = entry.get("tool") if isinstance(entry.get("tool"), dict) else {}
    name = _tool_name(entry)
    tool_call_id = entry.get("tool_call_id") or tool.get("id")
    codex_item = _codex_item(entry)
    input_value = entry.get("input", tool.get("input"))
    output_value = entry.get("output", tool.get("output"))
    if codex_item and codex_item.get("type") == "mcp_tool_call":
        input_value = input_value if input_value is not None else (
            codex_item.get("arguments") or codex_item.get("input")
        )
        output_value = output_value if output_value is not None else (
            codex_item.get("result") or codex_item.get("output")
        )
        if not tool_call_id and codex_item.get("id") is not None:
            tool_call_id = codex_item.get("id")
        if not error and codex_item.get("error") is not None:
            error = str(codex_item.get("error"))
    tool_use = _first_content_block(_claude_content_blocks(entry), "tool_use")
    if tool_use:
        if input_value is None:
            input_value = tool_use.get("input")
        if not tool_call_id and tool_use.get("id") is not None:
            tool_call_id = tool_use.get("id")
        if not name and tool_use.get("name"):
            name = str(tool_use["name"])
    result_fields = _claude_tool_result_fields(entry)
    if result_fields:
        result_output, result_call_id, is_error = result_fields
        if output_value is None:
            output_value = result_output
        if not tool_call_id and result_call_id:
            tool_call_id = result_call_id
        if not error and is_error:
            error = str(result_output) if result_output is not None else "tool error"
    return ToolPayload(
        name=name,
        input=_maybe_json(input_value),
        output=_maybe_json(output_value),
        tool_call_id=str(tool_call_id) if tool_call_id else None,
        error=error or entry.get("error"),
    )


def _agent_kind_and_title(entry: dict[str, Any]) -> tuple[EventKind, str, str]:
    event = str(entry.get("event") or "other")
    phase = entry.get("phase")
    item = _codex_item(entry)

    if event in {"item.started", "item.completed"} and item:
        item_type = str(item.get("type") or "")
        if item_type == "mcp_tool_call":
            name = str(item.get("tool") or item.get("name") or "tool")
            if event == "item.started":
                return (
                    "tool_call",
                    f"tool {name}",
                    _truncate(item.get("arguments") or item.get("input")),
                )
            if item.get("status") == "failed":
                return (
                    "tool_error",
                    f"tool error {name}",
                    _truncate(item.get("error")),
                )
            return (
                "tool_result",
                f"tool result {name}",
                _truncate(item.get("result") or item.get("output")),
            )
        if item_type == "agent_message":
            return "llm", "assistant", _truncate(item.get("text"))
        if item_type == "error":
            return (
                "system",
                "warning",
                _summarize_error_blob(item.get("message") or item.get("text")),
            )
        return "other", item_type or event, _truncate(item.get("text") or item)

    # Claude CLI stream-json: tool_use / tool_result / text live under claude_event.
    claude_blocks = _claude_content_blocks(entry)
    if claude_blocks:
        tool_use = _first_content_block(claude_blocks, "tool_use")
        if tool_use:
            name = str(tool_use.get("name") or "tool")
            return "tool_call", f"tool {name}", _truncate(tool_use.get("input"))
        tool_result = _first_content_block(claude_blocks, "tool_result")
        if tool_result:
            name = _tool_name(entry) or "tool"
            content = tool_result.get("content")
            if tool_result.get("is_error"):
                return "tool_error", f"tool error {name}", _truncate(content)
            return "tool_result", f"tool result {name}", _truncate(content)
        text = _claude_text(claude_blocks)
        if text or event == "assistant":
            return "llm", event if event != "other" else "assistant", _truncate(text)

    # Alternate Claude user/tool-result shape (string message + tool_use_result).
    result_fields = _claude_tool_result_fields(entry)
    if result_fields:
        output, _, is_error = result_fields
        name = _tool_name(entry) or "tool"
        if is_error:
            return "tool_error", f"tool error {name}", _truncate(output)
        return "tool_result", f"tool result {name}", _truncate(output)

    if event == "tool_start":
        name = _tool_name(entry) or "tool"
        return "tool_call", f"tool {name}", _truncate(entry.get("input"))
    if event == "tool_end":
        name = _tool_name(entry) or "tool"
        return "tool_result", f"tool result {name}", _truncate(entry.get("output"))
    if event in {"tool_error"}:
        name = _tool_name(entry) or "tool"
        return "tool_error", f"tool error {name}", _truncate(entry.get("error"))
    # Codex CLI turns — pair like llm_start / llm_end in the viewer.
    if event == "turn.started":
        return "llm", "turn", ""
    if event == "turn.completed":
        return "llm", "turn completed", _truncate(
            entry.get("text") or entry.get("messages")
        )
    if event == "turn.failed":
        return "llm", "turn failed", _summarize_error_blob(_codex_error_text(entry))
    if event in {"llm_start", "llm_end", "assistant"}:
        return "llm", event, _truncate(entry.get("text") or entry.get("messages"))
    if event == "error":
        # Stream reconnect chatter stays system-level; fold into the turn in UI.
        return "system", "error", _summarize_error_blob(_codex_error_text(entry))
    if event in {"llm_end_error", "agent_error"}:
        return "other", event, _summarize_error_blob(
            entry.get("error") or _codex_error_text(entry)
        )
    if event == "subprocess_error":
        return (
            "system",
            "subprocess_error",
            _summarize_subprocess_stderr(
                entry.get("stderr")
                or entry.get("error")
                or _codex_error_text(entry)
            ),
        )
    # Codex / CLI bookkeeping — keep off the Agent/Other flood.
    if event in {"mcp_config", "subprocess_start", "thread.started"}:
        return "system", event.replace(".", " "), _truncate(
            entry.get("command")
            or entry.get("servers")
            or entry.get("message")
        )
    # Phase bookends — same names across Codex / Claude / BYO / CLI.
    if event in {"agent_start", "agent_done"}:
        label = f"{event}" + (f" ({phase})" if phase else "")
        return "phase", label, _truncate(entry.get("message"))
    if event == "diagnosis_frozen":
        return "submission", "diagnosis frozen", _truncate(entry.get("report"))
    if event == "result":
        claude = _claude_event(entry) or {}
        return (
            "phase",
            "phase result",
            _truncate(claude.get("result")),
        )
    if event == "subprocess_nonzero_recovered":
        return (
            "other",
            event,
            _truncate(entry.get("stderr") or entry.get("message")),
        )
    return "other", event, _truncate(
        entry.get("message") or entry.get("text") or _codex_error_text(entry)
    )


def adapt_agent_event(entry: dict[str, Any], *, index: int) -> CanonicalTraceEvent:
    kind, title, summary = _agent_kind_and_title(entry)
    event = str(entry.get("event") or "other")
    tool = None
    if kind in {"tool_call", "tool_result", "tool_error"}:
        tool = _tool_from_entry(
            entry, error=str(entry["error"]) if entry.get("error") else None
        )
        if tool and (tool.input is not None or tool.output is not None or tool.error):
            summary = _truncate(tool.error or tool.output or tool.input) or summary

    return CanonicalTraceEvent(
        id=f"agent-{index}",
        timestamp=entry.get("timestamp"),
        source="agent",
        kind=kind,
        title=title,
        summary=summary,
        phase=entry.get("phase"),
        event=event,
        tool=tool,
        raw=_slim_raw(entry) if isinstance(entry, dict) else {},
    )


def adapt_nika_event(entry: dict[str, Any], *, index: int) -> CanonicalTraceEvent:
    event = str(entry.get("event") or "system")
    message = entry.get("message") or ""
    data = entry.get("data")
    if event in _LIFECYCLE_EVENTS:
        kind: EventKind = "lifecycle"
    elif event in {"eval_metrics_saved", "eval_publish"}:
        kind = "score"
    elif event == "system":
        kind = "system"
    else:
        kind = "lifecycle" if event else "other"

    summary_parts = [str(message)] if message else []
    if isinstance(data, dict) and data:
        # Prefer short diagnostic keys for the ledger row.
        for key in ("problem", "problem_name", "scenario", "host", "error"):
            if key in data:
                summary_parts.append(f"{key}={data[key]}")
                break
        else:
            summary_parts.append(_truncate(data, 120))

    duration_ms = entry.get("duration_ms")
    if duration_ms is None and isinstance(data, dict):
        duration_ms = data.get("duration_ms")
    try:
        duration_val = float(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration_val = None

    return CanonicalTraceEvent(
        id=f"nika-{index}",
        timestamp=entry.get("timestamp"),
        source="nika",
        kind=kind,
        title=event.replace("_", " "),
        summary=_truncate(" · ".join(summary_parts)),
        event=event,
        duration_ms=duration_val,
        raw=_slim_raw(entry) if isinstance(entry, dict) else {},
    )


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def load_agent_events(session_dir: Path) -> list[CanonicalTraceEvent]:
    path = session_dir / "messages.jsonl"
    return [
        adapt_agent_event(entry, index=i) for i, entry in enumerate(iter_jsonl(path))
    ]


def load_nika_events(session_dir: Path) -> list[CanonicalTraceEvent]:
    path = session_dir / "nika.jsonl"
    return [
        adapt_nika_event(entry, index=i) for i, entry in enumerate(iter_jsonl(path))
    ]
