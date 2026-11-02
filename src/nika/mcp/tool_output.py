"""Bound MCP tool output returned to agents (mini-swe-agent head/tail style).

Every tool result has the same character limit and truncation notice. Text
keeps its first and last characters; list results keep their first and last
items whole.
"""

from __future__ import annotations

import json
from typing import Any

# Agents read this note, so it names no host configuration.
_DESC_NOTE = (
    " Oversized outputs keep the first and last halves of the output budget "
    "(10000 characters by default) and elide the middle; list results elide "
    "whole items."
)

_TOO_LONG_WARNING = (
    "The output of your last tool call exceeded the output limit and was truncated."
)

_WARNING_BLOCK = f"<warning>\n{_TOO_LONG_WARNING}\n</warning>"

DEFAULT_MAX_CHARS = 10000


def tool_output_max_chars() -> int:
    """Return the observation char budget from run config."""
    try:
        from nika.run_config.loader import get_run_config

        return int(get_run_config().nika.mcp.tool_output_max_chars)
    except Exception:  # noqa: BLE001 - gateway / early import
        return DEFAULT_MAX_CHARS


def _resolve_max_chars(max_chars: int | None) -> int:
    return tool_output_max_chars() if max_chars is None else max_chars


def truncate_tool_text(text: str, *, max_chars: int | None = None) -> str:
    """Return *text*, or a head/tail preview with elision when over budget.

    Matches mini-swe-agent's default observation template: when longer than
    ``max_chars``, keep the first and last ``max_chars // 2`` characters
    (second half gets the remainder if odd), mark how many were elided, and
    warn the agent to use head/tail/grep or redirect to a file.
    """
    limit = _resolve_max_chars(max_chars)
    if limit <= 0 or len(text) <= limit:
        return text
    head_n = limit // 2
    tail_n = limit - head_n
    elided = len(text) - limit
    return (
        f"{_WARNING_BLOCK}\n"
        f"<output_head>\n{text[:head_n]}\n</output_head>\n"
        f"<elided_chars>\n{elided} characters elided\n</elided_chars>\n"
        f"<output_tail>\n{text[-tail_n:]}\n</output_tail>"
    )


def annotate_tools_list_result(result: dict[str, Any]) -> dict[str, Any]:
    """Annotate ``tools/list`` for truncation semantics."""
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        # structuredContent is dropped when output is truncated; without an
        # outputSchema, MCP clients do not require it.
        tool.pop("outputSchema", None)
        desc = tool.get("description")
        if isinstance(desc, str) and _DESC_NOTE.strip() not in desc:
            tool["description"] = desc.rstrip() + _DESC_NOTE
    return result


def bound_call_tool_result(
    result: dict[str, Any],
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Apply one head/tail budget across all text blocks of a ``tools/call`` result.

    Text blocks are joined with newlines into one observation string. When that
    string exceeds the budget, a list result (one text block per item) keeps
    its first and last items whole and elides the middle ones; any other
    result, or a list whose single items exceed the budget, is replaced by one
    truncated text block. ``structuredContent`` is removed and non-text content
    blocks are kept. Results within budget are returned unchanged.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return result
    limit = _resolve_max_chars(max_chars)
    texts = [
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    if not texts:
        return result
    combined = "\n".join(texts)
    if limit <= 0 or len(combined) <= limit:
        return result

    bounded = _bound_items(result, texts, limit) or [
        {"type": "text", "text": truncate_tool_text(combined, max_chars=limit)}
    ]
    for item in content:
        is_text = (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
        if not is_text:
            bounded.append(item)
    result["content"] = bounded
    result.pop("structuredContent", None)
    return result


def _bound_items(
    result: dict[str, Any], texts: list[str], limit: int
) -> list[Any] | None:
    """Keep whole first and last items of a list result within *limit* chars."""
    structured = result.get("structuredContent")
    items = structured.get("result") if isinstance(structured, dict) else None
    if not isinstance(items, list) or len(items) != len(texts) or len(texts) < 2:
        return None
    head: list[str] = []
    used = 0
    for text in texts:
        if used + len(text) + 1 > limit // 2:
            break
        head.append(text)
        used += len(text) + 1
    tail: list[str] = []
    for text in reversed(texts[len(head) :]):
        if used + len(text) + 1 > limit:
            break
        tail.insert(0, text)
        used += len(text) + 1
    if not head and not tail:
        return None
    elided = len(texts) - len(head) - len(tail)
    notice = f"<elided_items>\n{elided} of {len(texts)} items elided\n</elided_items>"
    return [
        {"type": "text", "text": text}
        for text in (_WARNING_BLOCK, *head, notice, *tail)
    ]


def rewrite_jsonrpc_payload(
    payload: dict[str, Any],
    *,
    max_chars: int | None = None,
    bound_call: bool = False,
) -> dict[str, Any]:
    """Rewrite a JSON-RPC response for ``tools/list`` or ``tools/call``."""
    if "result" not in payload or not isinstance(payload.get("result"), dict):
        return payload
    result = payload["result"]
    if "tools" in result:
        payload["result"] = annotate_tools_list_result(result)
        return payload
    if "content" in result and bound_call:
        payload["result"] = bound_call_tool_result(result, max_chars=max_chars)
    return payload


def rewrite_http_body(
    body: bytes,
    *,
    content_type: str,
    max_chars: int | None = None,
    bound_call: bool = False,
) -> bytes:
    """Rewrite a JSON or SSE MCP HTTP response body."""
    ct = content_type.lower()
    if "text/event-stream" in ct:
        return _rewrite_sse_body(body, max_chars=max_chars, bound_call=bound_call)
    if "application/json" in ct or not ct:
        return _rewrite_json_body(body, max_chars=max_chars, bound_call=bound_call)
    return body


def _rewrite_json_body(
    body: bytes,
    *,
    max_chars: int | None,
    bound_call: bool,
) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    rewritten = rewrite_jsonrpc_payload(
        payload, max_chars=max_chars, bound_call=bound_call
    )
    return json.dumps(rewritten, ensure_ascii=False).encode("utf-8")


def _rewrite_sse_body(
    body: bytes,
    *,
    max_chars: int | None,
    bound_call: bool,
) -> bytes:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("data:"):
            raw = line[5:].lstrip()
            ending = ""
            if raw.endswith("\r\n"):
                ending = "\r\n"
                raw = raw[:-2]
            elif raw.endswith("\n"):
                ending = "\n"
                raw = raw[:-1]
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                out_lines.append(line)
                continue
            if isinstance(payload, dict):
                payload = rewrite_jsonrpc_payload(
                    payload, max_chars=max_chars, bound_call=bound_call
                )
                out_lines.append(
                    "data: "
                    + json.dumps(payload, ensure_ascii=False)
                    + (ending or "\n")
                )
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)
    return "".join(out_lines).encode("utf-8")


def jsonrpc_method(body: bytes) -> str | None:
    """Return the JSON-RPC method name for an MCP POST body, if any."""
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    return str(method) if method else None
