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
        "agent_start",
        "agent_done",
        "agent_end",
        "agent_error",
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
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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
    claude = entry.get("claude_event") or {}
    message = claude.get("message") if isinstance(claude, dict) else None
    content = (message or {}).get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if name:
                    return str(name)
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
        return "other", item_type or event, _truncate(item.get("text") or item)

    if event == "tool_start":
        name = _tool_name(entry) or "tool"
        return "tool_call", f"tool {name}", _truncate(entry.get("input"))
    if event == "tool_end":
        name = _tool_name(entry) or "tool"
        return "tool_result", f"tool result {name}", _truncate(entry.get("output"))
    if event in {"tool_error"}:
        name = _tool_name(entry) or "tool"
        return "tool_error", f"tool error {name}", _truncate(entry.get("error"))
    if event in {"llm_start", "llm_end", "assistant", "turn.completed"}:
        return "llm", event, _truncate(entry.get("text") or entry.get("messages"))
    if event in {"llm_end_error", "agent_error", "subprocess_error"}:
        return "other", event, _truncate(entry.get("error"))
    if event in {"agent_start", "agent_done"}:
        label = f"{event}" + (f" ({phase})" if phase else "")
        return "phase", label, _truncate(entry.get("message"))
    if event == "diagnosis_frozen":
        return "submission", "diagnosis frozen", _truncate(entry.get("report"))
    if event == "result":
        return (
            "phase",
            "phase result",
            _truncate((entry.get("claude_event") or {}).get("result")),
        )
    return "other", event, _truncate(entry.get("message") or entry.get("text"))


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
        raw=entry,
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
        raw=entry,
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
