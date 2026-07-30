"""Human-readable formatting for ``codex exec --json`` JSONL events."""

from __future__ import annotations

import json
from typing import Any


def format_codex_event(event: dict[str, Any]) -> str | None:
    """Return a terminal-friendly line for a Codex JSONL event, or None to skip."""
    event_type = event.get("type", "")

    if event_type == "thread.started":
        thread_id = event.get("thread_id", "")
        suffix = f" ({thread_id[:8]}…)" if thread_id else ""
        return f"▶ Codex thread started{suffix}"

    if event_type == "turn.started":
        return "↻ Turn started"

    if event_type == "turn.completed":
        usage = event.get("usage") or {}
        if usage:
            return (
                "✓ Turn completed "
                f"(in={usage.get('input_tokens', '?')}, "
                f"out={usage.get('output_tokens', '?')})"
            )
        return "✓ Turn completed"

    if event_type == "turn.failed":
        message = (event.get("error") or {}).get("message", "unknown error")
        return f"✗ Turn failed: {message}"

    if event_type == "error":
        message = event.get("message", "")
        if "Reconnecting" in message:
            return f"… {message}"
        return f"✗ Error: {message}"

    if event_type.startswith("item."):
        return _format_item_event(event_type, event.get("item") or {})

    return None


def _format_item_event(phase: str, item: dict[str, Any]) -> str | None:
    item_type = item.get("type", "")

    if item_type == "agent_message" and phase == "item.completed":
        text = (item.get("text") or "").strip()
        return f"\n💬 Agent:\n{text}\n" if text else None

    if item_type == "reasoning" and phase == "item.completed":
        text = (item.get("text") or "").strip()
        return f"💭 {text}" if text else None

    if item_type == "command_execution":
        command = item.get("command", "")
        if phase == "item.started":
            return f"⚙ Running: {command}"
        if phase == "item.completed":
            exit_code = item.get("exit_code")
            output = (item.get("aggregated_output") or "").strip()
            lines = [f"⚙ Done: {command} (exit {exit_code})"]
            if output:
                lines.append(_indent(_truncate(output, 500)))
            return "\n".join(lines)
        return None

    if item_type == "mcp_tool_call":
        server = item.get("server", "")
        tool = item.get("tool", "")
        label = f"{server}/{tool}" if server else tool
        if phase == "item.started":
            args = item.get("arguments")
            suffix = ""
            if args is not None:
                args_preview = _truncate(json.dumps(args, ensure_ascii=False), 120)
                suffix = f" {args_preview}"
            return f"🔧 MCP {label}{suffix}"
        if phase == "item.completed":
            if item.get("status") == "failed":
                message = (item.get("error") or {}).get("message", "failed")
                return f"✗ MCP {label}: {message}"
            result_text = _extract_mcp_result_text(item.get("result"))
            lines = [f"✓ MCP {label}"]
            if result_text:
                lines.append(_indent(_truncate(result_text, 800)))
            return "\n".join(lines)
        return None

    if item_type == "file_change" and phase == "item.completed":
        changes = item.get("changes") or []
        if not changes:
            return "📄 File change (no paths)"
        parts = [
            f"{change.get('kind', '?')} {change.get('path', '?')}" for change in changes
        ]
        return "📄 " + ", ".join(parts)

    if item_type == "web_search" and phase == "item.completed":
        query = item.get("query", "")
        return f"🔍 Web search: {query}" if query else None

    if item_type == "todo_list":
        items = item.get("items") or []
        if not items:
            return None
        lines = ["📋 Plan:"]
        for entry in items:
            mark = "✓" if entry.get("completed") else "○"
            lines.append(f"   {mark} {entry.get('text', '')}")
        return "\n".join(lines)

    if item_type == "error" and phase == "item.completed":
        message = item.get("message", "")
        return f"⚠ {message}" if message else None

    return None


def _extract_mcp_result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        structured = result.get("structured_content")
        if structured is not None:
            return _truncate(json.dumps(structured, ensure_ascii=False, indent=2), 800)
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif block.get("type") == "resource":
            resource = block.get("resource") or {}
            if resource.get("text"):
                parts.append(str(resource["text"]))
    return "\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _indent(text: str) -> str:
    return "\n".join(f"   {line}" for line in text.splitlines())
