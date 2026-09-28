"""Unit tests for MCP tool-output bounds (mini-swe-agent head/tail)."""

from __future__ import annotations

import json

import pytest

from nika.mcp.tool_output import (
    annotate_tools_list_result,
    bound_call_tool_result,
    rewrite_http_body,
    truncate_tool_text,
)


def test_truncate_under_limit_unchanged() -> None:
    assert truncate_tool_text("a" * 100, max_chars=200) == "a" * 100


def test_truncate_keeps_head_and_tail_with_warning() -> None:
    text = "H" * 20 + "M" * 30 + "T" * 20
    out = truncate_tool_text(text, max_chars=40)
    assert "too long" in out
    assert "head, tail or sed" in out
    assert "redirect output to a file" in out
    assert "30 characters elided" in out
    head = out.split("<output_head>")[1].split("</output_head>")[0].strip()
    tail = out.split("<output_tail>")[1].split("</output_tail>")[0].strip()
    assert head == "H" * 20
    assert tail == "T" * 20


def test_truncate_default_budget_matches_mini_swe() -> None:
    out = truncate_tool_text("x" * 12000, max_chars=10000)
    head = out.split("<output_head>")[1].split("</output_head>")[0].strip()
    tail = out.split("<output_tail>")[1].split("</output_tail>")[0].strip()
    assert len(head) == 5000
    assert len(tail) == 5000
    assert "2000 characters elided" in out


def test_truncate_disabled_when_limit_zero() -> None:
    text = "w" * 10000
    assert truncate_tool_text(text, max_chars=0) == text


def test_bound_call_joins_blocks_drops_structured_content() -> None:
    result = {
        "content": [{"type": "text", "text": "x" * 10} for _ in range(5)],
        "structuredContent": {"result": ["x" * 10] * 5},
    }
    out = bound_call_tool_result(result, max_chars=20)
    assert len(out["content"]) == 1
    assert out["content"][0]["text"].startswith("<warning>")
    assert "34 characters elided" in out["content"][0]["text"]
    assert "structuredContent" not in out


def test_bound_call_within_budget_unchanged() -> None:
    result = {
        "content": [{"type": "text", "text": "ok"}, {"type": "text", "text": "ok"}],
        "structuredContent": {"result": ["ok", "ok"]},
    }
    expected = json.loads(json.dumps(result))
    assert bound_call_tool_result(result, max_chars=10) == expected


def test_tools_list_drops_output_schema_and_notes_truncation() -> None:
    result = {
        "tools": [
            {
                "name": "exec_shell",
                "description": "Run a command.",
                "inputSchema": {"type": "object", "properties": {}},
                "outputSchema": {"type": "object"},
            }
        ]
    }
    tool = annotate_tools_list_result(result)["tools"][0]
    assert "outputSchema" not in tool
    assert "elide the middle" in tool["description"]


def test_rewrite_sse_tools_call() -> None:
    result = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [{"type": "text", "text": "b" * 60}], "isError": False},
    }
    sse = f"event: message\ndata: {json.dumps(result)}\n\n".encode()
    out = rewrite_http_body(
        sse, content_type="text/event-stream", max_chars=20, bound_call=True
    )
    data_line = next(line for line in out.decode().splitlines() if line.startswith("data:"))
    payload = json.loads(data_line[5:].lstrip())
    assert payload["result"]["content"][0]["text"].startswith("<warning>")


@pytest.mark.asyncio
async def test_gateway_rewriting_send_applies_budget(monkeypatch) -> None:
    from nika.mcp.gateway import middleware as mw

    monkeypatch.setattr(mw, "tool_output_max_chars", lambda: 40)
    messages: list[dict] = []

    async def capture(message):
        messages.append(message)

    send = mw._RewritingSend(capture, bound_call=True)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "c" * 80}], "isError": False},
    }
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body[:40], "more_body": True})
    await send({"type": "http.response.body", "body": body[40:]})

    text = json.loads(messages[1]["body"])["result"]["content"][0]["text"]
    assert text.startswith("<warning>")
    assert "40 characters elided" in text
