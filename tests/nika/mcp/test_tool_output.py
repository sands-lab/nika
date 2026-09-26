"""Unit tests for MCP tool-output bounds (truncate + full)."""

from __future__ import annotations

import json

import pytest

from nika.mcp.tool_output import (
    FULL_ARG,
    bound_call_tool_result,
    inject_full_into_tools_list_result,
    pop_full_arg,
    rewrite_http_body,
    rewrite_jsonrpc_payload,
    strip_full_from_tools_call_body,
    truncate_tool_text,
)


def test_truncate_under_limit_unchanged() -> None:
    text = "a" * 100
    assert truncate_tool_text(text, max_chars=200, full_max_chars=1000) == text


def test_truncate_over_limit_leading_banner() -> None:
    text = "x" * 50
    out = truncate_tool_text(text, max_chars=20, full_max_chars=100)
    assert out.startswith("[TRUNCATED]")
    assert "first 20 of 50" in out
    assert "full=true" in out
    assert out.endswith("x" * 20)


def test_truncate_full_uses_larger_budget() -> None:
    text = "y" * 80
    out = truncate_tool_text(text, full=True, max_chars=20, full_max_chars=50)
    assert out.startswith("[TRUNCATED]")
    assert "full=true still capped at 50" in out
    assert out.endswith("y" * 50)


def test_truncate_full_within_full_budget() -> None:
    text = "z" * 40
    out = truncate_tool_text(text, full=True, max_chars=10, full_max_chars=100)
    assert out == text


def test_truncate_disabled_when_limit_zero() -> None:
    text = "w" * 10000
    assert truncate_tool_text(text, max_chars=0, full_max_chars=0) == text


def test_pop_full_arg() -> None:
    args = {"device_name": "n1", "full": True}
    assert pop_full_arg(args) is True
    assert args == {"device_name": "n1"}
    assert pop_full_arg({"full": "yes"}) is True
    assert pop_full_arg({}) is False


def test_inject_full_into_tools_list() -> None:
    result = {
        "tools": [
            {
                "name": "srl_show_ip_route",
                "description": "Get routes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"device_name": {"type": "string"}},
                    "required": ["device_name"],
                },
            }
        ]
    }
    out = inject_full_into_tools_list_result(result)
    tool = out["tools"][0]
    assert FULL_ARG in tool["inputSchema"]["properties"]
    assert "full=true" in tool["description"]
    assert "device_name" in tool["inputSchema"]["required"]
    assert FULL_ARG not in tool["inputSchema"]["required"]


def test_bound_call_tool_result_truncates_text() -> None:
    result = {
        "content": [{"type": "text", "text": "a" * 100}],
        "isError": False,
    }
    out = bound_call_tool_result(result, full=False, max_chars=30, full_max_chars=200)
    text = out["content"][0]["text"]
    assert text.startswith("[TRUNCATED]")
    assert text.endswith("a" * 30)


def test_strip_full_from_tools_call_body() -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "srl_show_ip_route",
                "arguments": {"device_name": "n1", "full": True},
            },
        }
    ).encode()
    new_body, full = strip_full_from_tools_call_body(body)
    assert full is True
    payload = json.loads(new_body)
    assert payload["params"]["arguments"] == {"device_name": "n1"}


def test_rewrite_json_tools_list() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "tools": [
                {
                    "name": "exec_shell",
                    "description": "Run a command.",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]
        },
    }
    body = json.dumps(payload).encode()
    out = rewrite_http_body(body, content_type="application/json", full=None)
    rewritten = json.loads(out)
    assert FULL_ARG in rewritten["result"]["tools"][0]["inputSchema"]["properties"]


def test_rewrite_sse_tools_call() -> None:
    result = {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": [{"type": "text", "text": "b" * 60}], "isError": False},
    }
    sse = f"event: message\ndata: {json.dumps(result)}\n\n".encode()
    out = rewrite_http_body(
        sse,
        content_type="text/event-stream",
        full=False,
        max_chars=20,
        full_max_chars=100,
    )
    text = out.decode()
    assert "data: " in text
    data_line = next(line for line in text.splitlines() if line.startswith("data:"))
    payload = json.loads(data_line[5:].lstrip())
    assert payload["result"]["content"][0]["text"].startswith("[TRUNCATED]")


@pytest.mark.asyncio
async def test_rewriting_send_buffers_json_response(monkeypatch) -> None:
    from nika.mcp.gateway import middleware as mw

    monkeypatch.setattr(mw, "tool_output_limits", lambda: (32, 100))

    messages: list[dict] = []

    async def capture(message):
        messages.append(message)

    send = mw._RewritingSend(capture, full=False)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": "c" * 80}],
            "isError": False,
        },
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

    assert messages[0]["type"] == "http.response.start"
    rewritten = json.loads(messages[1]["body"])
    text = rewritten["result"]["content"][0]["text"]
    assert text.startswith("[TRUNCATED]")
    assert "first 32 of 80" in text
    assert text.endswith("c" * 32)


def test_rewrite_jsonrpc_ignores_errors() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "x"}}
    assert rewrite_jsonrpc_payload(payload, full=False) == payload


def test_budget_is_cumulative_across_blocks_and_drops_structured() -> None:
    blocks = [{"type": "text", "text": "x" * 10} for _ in range(5)]
    result = {"content": blocks, "structuredContent": {"result": ["x" * 10] * 5}}
    out = bound_call_tool_result(result, full=False, max_chars=25, full_max_chars=100)
    texts = [item["text"] for item in out["content"]]
    assert len(texts) == 3
    assert texts[0].startswith("[TRUNCATED] Showing first 25 of 50 chars.")
    assert sum(len(t) for t in texts) - (len(texts[0]) - 10) == 25
    assert "structuredContent" not in out


def test_within_budget_result_is_unchanged() -> None:
    result = {
        "content": [{"type": "text", "text": "ok"}, {"type": "text", "text": "ok"}],
        "structuredContent": {"result": ["ok", "ok"]},
    }
    expected = json.loads(json.dumps(result))
    assert (
        bound_call_tool_result(result, full=False, max_chars=10, full_max_chars=20)
        == expected
    )


def test_tools_list_drops_output_schema() -> None:
    result = {"tools": [{"name": "t", "inputSchema": {}, "outputSchema": {}}]}
    assert "outputSchema" not in inject_full_into_tools_list_result(result)["tools"][0]
