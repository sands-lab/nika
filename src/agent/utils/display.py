"""Shared terminal presentation of agent conversation events."""

from __future__ import annotations

import ast
import json
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_shown: ContextVar[dict[str, Any] | None] = ContextVar("agent_shown", default=None)


@contextmanager
def agent_output(enabled: bool) -> Iterator[None]:
    shown_token = _shown.set(
        {"text": [], "reasoning_content": [], "tools": {}} if enabled else None
    )
    try:
        yield
    finally:
        _shown.reset(shown_token)


def _text(value: Any, *, content_blocks: bool = False) -> str:
    if content_blocks and isinstance(value, str) and value.lstrip().startswith("[{"):
        # SDKs may persist MCP blocks as JSON or a Python repr string.
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            try:
                value = ast.literal_eval(value)
            except (ValueError, SyntaxError, RecursionError):
                return value
    if (
        content_blocks
        and isinstance(value, list)
        and all(isinstance(block, dict) and "type" in block for block in value)
    ):
        parts = []
        for block in value:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "resource":
                resource = block.get("resource") or {}
                parts.append(str(resource.get("text") or resource.get("uri", "")))
            else:
                parts.append(f"[{block['type']}]")
        return "\n".join(parts)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _preview(value: Any, limit: int) -> str:
    text = _text(value).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_agent_event(event: str, payload: dict[str, Any]) -> str | None:
    """Format canonical model/tool events using the same labels and limits."""
    if event == "llm_end":
        parts = []
        reasoning = payload.get("reasoning_content")
        if reasoning:
            parts.append(f"💭 Thinking:\n{_preview(reasoning, 500)}")
        text = payload.get("text")
        if text and _text(text).strip():
            parts.append(f"💬 Agent:\n{_text(text).strip()}")
        return "\n".join(parts) or None
    if event in {"tool_start", "tool_end", "tool_error"}:
        name = (payload.get("tool") or {}).get("name") or "tool"
        if name.startswith("mcp__"):
            name = name.split("__", 2)[-1]
        if event == "tool_start":
            return f"🔧 Tool {name}: {_preview(payload.get('input', ''), 200)}"
        output = _text(
            payload.get("error", payload.get("output", "")), content_blocks=True
        )
        mark = "✗" if event == "tool_error" else "✓"
        return f"{mark} Tool {name}\n{_preview(output, 800)}"
    if event in {"llm_end_error", "llm_retry"}:
        return f"{'↻ Retry' if event == 'llm_retry' else '✗ Model error'}: {_preview(payload.get('error', payload), 500)}"
    return None


def display_agent_event(event: str, payload: dict[str, Any]) -> None:
    shown = _shown.get()
    if shown is None:
        return
    if event in {"model_text", "model_reasoning"}:
        field = "text" if event == "model_text" else "reasoning_content"
        value = payload.get(field, "")
        if value:
            shown[field].append(value)
            rendered = format_agent_event("llm_end", {field: value})
            if rendered:
                print(rendered, file=sys.stderr, flush=True)
        return
    if event == "llm_end":
        payload = dict(payload)
        for field in ("text", "reasoning_content"):
            separator = "\n" if field == "text" else "\n\n"
            if (
                shown[field]
                and str(payload.get(field, "")).strip()
                == separator.join(shown[field]).strip()
            ):
                payload.pop(field, None)
            shown[field].clear()
    # Claude CLI persists its native stream; SDKs and BYO agents use canonical
    # events. Adapt only for display, preserving the trajectory on disk.
    native = payload.get("claude_event")
    if isinstance(native, dict):
        kind = native.get("type")
        if kind in {"assistant", "user"}:
            for block in (native.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type in {"text", "thinking"}:
                    display_agent_event(
                        "llm_end",
                        {
                            "text"
                            if block_type == "text"
                            else "reasoning_content": block.get(block_type, "")
                        },
                    )
                elif block_type == "tool_use":
                    shown["tools"][block.get("id")] = block.get("name", "tool")
                    display_agent_event(
                        "tool_start",
                        {
                            "tool": {"name": block.get("name", "tool")},
                            "input": block.get("input", {}),
                        },
                    )
                elif block_type == "tool_result":
                    display_agent_event(
                        "tool_error" if block.get("is_error") else "tool_end",
                        {
                            "tool": {
                                "name": shown["tools"].pop(
                                    block.get("tool_use_id"), "tool"
                                )
                            },
                            "output": block.get("content", ""),
                        },
                    )
        elif kind == "result" and native.get("is_error"):
            display_agent_event(
                "llm_end_error", {"error": native.get("result", "unknown error")}
            )
        return
    native = payload.get("codex_event")
    if isinstance(native, dict):
        item = native.get("item") or {}
        if native.get("type") == "item.completed" and item.get("type") in {
            "agent_message",
            "reasoning",
        }:
            field = "text" if item["type"] == "agent_message" else "reasoning_content"
            display_agent_event(
                "model_text" if field == "text" else "model_reasoning",
                {field: item.get("text", "")},
            )
        # Tool events have canonical mirrors.
        if native.get("type") in {"error", "turn.failed"}:
            display_agent_event(
                "llm_end_error", {"error": native.get("message") or native.get("error")}
            )
        return
    rendered = format_agent_event(event, payload)
    if rendered:
        print(rendered, file=sys.stderr, flush=True)
