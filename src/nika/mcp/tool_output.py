"""Bound MCP tool text returned to agents (truncate + full opt-in)."""

from __future__ import annotations

import json
from typing import Any

FULL_ARG = "full"

_FULL_SCHEMA_PROP: dict[str, Any] = {
    "type": "boolean",
    "default": False,
    "description": (
        "If true, request a larger tool-output budget. "
        "Oversized outputs are still hard-capped."
    ),
}

_DESC_NOTE = (
    " Oversized outputs are truncated unless full=true "
    "(larger budget, still hard-capped)."
)

DEFAULT_MAX_CHARS = 16384
DEFAULT_FULL_MAX_CHARS = 100000


def tool_output_limits() -> tuple[int, int]:
    """Return ``(max_chars, full_max_chars)`` from run config."""
    try:
        from nika.run_config.loader import get_run_config

        mcp = get_run_config().nika.mcp
        return int(mcp.tool_output_max_chars), int(mcp.tool_output_full_max_chars)
    except Exception:  # noqa: BLE001 - gateway / early import
        return DEFAULT_MAX_CHARS, DEFAULT_FULL_MAX_CHARS


def _resolve_limits(
    max_chars: int | None, full_max_chars: int | None
) -> tuple[int, int]:
    if max_chars is None or full_max_chars is None:
        cfg_max, cfg_full = tool_output_limits()
        if max_chars is None:
            max_chars = cfg_max
        if full_max_chars is None:
            full_max_chars = cfg_full
    return max_chars, full_max_chars


def _truncation_banner(
    *, limit: int, total: int, full: bool, full_max_chars: int
) -> str:
    if full:
        return (
            f"[TRUNCATED] Showing first {limit} of {total} chars "
            f"(full=true still capped at {full_max_chars}).\n\n"
        )
    return (
        f"[TRUNCATED] Showing first {limit} of {total} chars. "
        f"Re-call with full=true for more "
        f"(capped at {full_max_chars}).\n\n"
    )


def truncate_tool_text(
    text: str,
    *,
    full: bool = False,
    max_chars: int | None = None,
    full_max_chars: int | None = None,
) -> str:
    """Return *text*, or a leading-TRUNCATED preview when over budget."""
    max_chars, full_max_chars = _resolve_limits(max_chars, full_max_chars)
    limit = full_max_chars if full else max_chars
    if limit <= 0 or len(text) <= limit:
        return text
    banner = _truncation_banner(
        limit=limit, total=len(text), full=full, full_max_chars=full_max_chars
    )
    return banner + text[:limit]


def pop_full_arg(arguments: dict[str, Any]) -> bool:
    """Remove and return the gateway ``full`` flag from tool arguments."""
    if FULL_ARG not in arguments:
        return False
    value = arguments.pop(FULL_ARG)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def inject_full_into_tools_list_result(result: dict[str, Any]) -> dict[str, Any]:
    """Add optional ``full`` to each tool schema in a ``tools/list`` result."""
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        desc = tool.get("description")
        # structuredContent is dropped when output is truncated; without an
        # outputSchema, MCP clients do not require it.
        tool.pop("outputSchema", None)
        if isinstance(desc, str) and _DESC_NOTE.strip() not in desc:
            tool["description"] = desc.rstrip() + _DESC_NOTE
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
            tool["inputSchema"] = schema
        props = schema.get("properties")
        if not isinstance(props, dict):
            props = {}
            schema["properties"] = props
        props.setdefault(FULL_ARG, dict(_FULL_SCHEMA_PROP))
    return result


def bound_call_tool_result(
    result: dict[str, Any],
    *,
    full: bool,
    max_chars: int | None = None,
    full_max_chars: int | None = None,
) -> dict[str, Any]:
    """Apply one text budget across all text blocks of a ``tools/call`` result.

    A tool that returns a list gets one content block per item, so the budget
    is cumulative: blocks are kept in order until it runs out, the block that
    crosses it is cut, and later text blocks are dropped.  When anything is
    cut, ``structuredContent`` (an untruncated copy of the same data) is
    removed too.  Results within budget are returned unchanged.
    """
    content = result.get("content")
    if not isinstance(content, list):
        return result
    max_chars, full_max_chars = _resolve_limits(max_chars, full_max_chars)
    limit = full_max_chars if full else max_chars
    texts = [
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    total = sum(len(text) for text in texts)
    if limit <= 0 or total <= limit:
        return result

    banner = _truncation_banner(
        limit=limit, total=total, full=full, full_max_chars=full_max_chars
    )
    remaining = limit
    bounded: list[Any] = []
    for item in content:
        is_text = (
            isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        )
        if not is_text:
            bounded.append(item)
            continue
        if remaining <= 0:
            continue
        text = item["text"][:remaining]
        remaining -= len(text)
        if banner:
            text, banner = banner + text, ""
        bounded.append({**item, "text": text})
    result["content"] = bounded
    result.pop("structuredContent", None)
    return result


def rewrite_jsonrpc_payload(
    payload: dict[str, Any],
    *,
    full: bool | None = None,
    max_chars: int | None = None,
    full_max_chars: int | None = None,
) -> dict[str, Any]:
    """Rewrite a JSON-RPC response for ``tools/list`` or ``tools/call``."""
    if "result" not in payload or not isinstance(payload.get("result"), dict):
        return payload
    result = payload["result"]
    if "tools" in result:
        payload["result"] = inject_full_into_tools_list_result(result)
        return payload
    if "content" in result and full is not None:
        payload["result"] = bound_call_tool_result(
            result,
            full=full,
            max_chars=max_chars,
            full_max_chars=full_max_chars,
        )
    return payload


def rewrite_http_body(
    body: bytes,
    *,
    content_type: str,
    full: bool | None = None,
    max_chars: int | None = None,
    full_max_chars: int | None = None,
) -> bytes:
    """Rewrite a JSON or SSE MCP HTTP response body."""
    ct = content_type.lower()
    if "text/event-stream" in ct:
        return _rewrite_sse_body(
            body, full=full, max_chars=max_chars, full_max_chars=full_max_chars
        )
    if "application/json" in ct or not ct:
        return _rewrite_json_body(
            body, full=full, max_chars=max_chars, full_max_chars=full_max_chars
        )
    return body


def _rewrite_json_body(
    body: bytes,
    *,
    full: bool | None,
    max_chars: int | None,
    full_max_chars: int | None,
) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict):
        return body
    rewritten = rewrite_jsonrpc_payload(
        payload, full=full, max_chars=max_chars, full_max_chars=full_max_chars
    )
    return json.dumps(rewritten, ensure_ascii=False).encode("utf-8")


def _rewrite_sse_body(
    body: bytes,
    *,
    full: bool | None,
    max_chars: int | None,
    full_max_chars: int | None,
) -> bytes:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("data:"):
            raw = line[5:].lstrip()
            # Preserve trailing newline from splitlines(keepends=True) separately.
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
                    payload,
                    full=full,
                    max_chars=max_chars,
                    full_max_chars=full_max_chars,
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


def strip_full_from_tools_call_body(body: bytes) -> tuple[bytes, bool | None]:
    """Strip ``full`` from a ``tools/call`` request body.

    Returns ``(new_body, full_flag)``. ``full_flag`` is ``None`` when the body
    is not a ``tools/call`` request.
    """
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body, None
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return body, None
    params = payload.get("params")
    if not isinstance(params, dict):
        return body, False
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return body, False
    arguments = dict(arguments)
    full = pop_full_arg(arguments)
    params = dict(params)
    params["arguments"] = arguments
    payload = dict(payload)
    payload["params"] = params
    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), full


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
