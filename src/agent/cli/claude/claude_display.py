"""Human-readable formatting for ``claude -p --output-format stream-json`` events."""

from __future__ import annotations

import json
from typing import Any


def should_log_claude_event(event: dict[str, Any]) -> bool:
    """Return False for streaming noise that should not be written to messages.jsonl."""
    return not (
        event.get("type") == "system" and event.get("subtype") == "thinking_tokens"
    )


def format_claude_event(event: dict[str, Any]) -> str | None:
    """Return a terminal-friendly line for a Claude stream-json event, or None to skip."""
    event_type = event.get("type", "")

    if event_type == "system":
        return _format_system_event(event)

    if event_type == "assistant":
        return _format_assistant_event(event)

    if event_type == "result":
        return _format_result_event(event)

    return None


def _format_system_event(event: dict[str, Any]) -> str | None:
    subtype = event.get("subtype", "")
    if subtype == "init":
        model = event.get("model", "")
        raw_servers = event.get("mcp_servers") or []
        # mcp_servers may be a list of strings or a list of dicts with a "name" key.
        server_names = [
            s["name"] if isinstance(s, dict) else str(s) for s in raw_servers
        ]
        suffix = f" | mcp: {', '.join(server_names)}" if server_names else ""
        return f"▶ Claude session started (model={model}){suffix}"
    # Skip noisy thinking_tokens and other subtypes.
    return None


def _format_assistant_event(event: dict[str, Any]) -> str | None:
    message = event.get("message") or {}
    content = message.get("content") or []
    parts: list[str] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")

        if block_type == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(f"\n💬 Agent:\n{text}\n")

        elif block_type == "thinking":
            text = (block.get("thinking") or "").strip()
            if text:
                parts.append(f"💭 {_truncate(text, 200)}")

        elif block_type == "tool_use":
            name = block.get("name", "")
            inp = block.get("input") or {}
            args_preview = _truncate(json.dumps(inp, ensure_ascii=False), 120)
            parts.append(f"🔧 MCP {name} {args_preview}")

    return "\n".join(parts) if parts else None


def _format_result_event(event: dict[str, Any]) -> str | None:
    is_error = event.get("is_error", False)
    usage = event.get("usage") or {}
    input_tokens = usage.get("input_tokens", "?")
    output_tokens = usage.get("output_tokens", "?")
    if is_error:
        result = event.get("result", "unknown error")
        return f"✗ Claude error: {_truncate(str(result), 200)}"
    return f"✓ Claude completed (in={input_tokens}, out={output_tokens})"


def _extract_tool_result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
