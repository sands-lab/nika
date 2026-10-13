import asyncio
import json

import pytest

from agent.sandbox.model_proxy import (
    _restore_stream,
    flatten_namespace_tools,
    fold_system_turns,
    restore_tool_namespaces,
    tool_namespaces,
)

pytestmark = pytest.mark.unit


def test_body_without_system_turns_is_unchanged() -> None:
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert fold_system_turns(body) is body


def test_system_turn_after_user_is_appended_to_that_user_turn() -> None:
    body = {
        "system": "top-level prompt",
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "system", "content": "reminder"},
        ],
    }
    folded = fold_system_turns(body)
    assert folded["system"] == "top-level prompt"
    assert folded["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "q"},
                {
                    "type": "text",
                    "text": "<system-reminder>\nreminder\n</system-reminder>",
                },
            ],
        }
    ]


def test_system_turn_after_assistant_moves_to_next_user_turn() -> None:
    tool_result = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            {"role": "system", "content": [{"type": "text", "text": "note"}]},
            {"role": "user", "content": [tool_result]},
        ]
    }
    messages = fold_system_turns(body)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] == [
        tool_result,
        {"type": "text", "text": "<system-reminder>\nnote\n</system-reminder>"},
    ]


def test_trailing_system_turn_becomes_user_turn() -> None:
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "system", "content": "late"},
        ]
    }
    messages = fold_system_turns(body)["messages"]
    assert messages[-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "<system-reminder>\nlate\n</system-reminder>"}
        ],
    }


def test_body_without_namespace_tools_is_unchanged() -> None:
    body = {"tools": [{"type": "function", "name": "exec_command"}]}
    assert flatten_namespace_tools(body) is body


def _codex_body() -> dict:
    return {
        "model": "gpt-oss:120b",
        "tools": [
            {"type": "function", "name": "exec_command"},
            {
                "type": "namespace",
                "name": "kathara",
                "tools": [{"type": "function", "name": "exec_shell"}],
            },
            {"type": "web_search"},
            {
                "type": "namespace",
                "name": "pingmesh",
                "tools": [{"type": "function", "name": "run_pingmesh_snapshot"}],
            },
        ],
        "input": [
            {"type": "message", "role": "user", "content": "go"},
            {
                "type": "function_call",
                "name": "exec_shell",
                "namespace": "kathara",
                "arguments": "{}",
                "call_id": "c1",
            },
        ],
    }


def test_namespace_tools_become_top_level_functions_in_order() -> None:
    flat = flatten_namespace_tools(_codex_body())
    assert flat["model"] == "gpt-oss:120b"
    assert flat["tools"] == [
        {"type": "function", "name": "exec_command"},
        {"type": "function", "name": "exec_shell"},
        {"type": "web_search"},
        {"type": "function", "name": "run_pingmesh_snapshot"},
    ]
    # The endpoint never saw namespaces, so resent calls drop theirs.
    assert "namespace" not in flat["input"][1]
    assert flat["input"][0] == {"type": "message", "role": "user", "content": "go"}


def test_tool_namespaces_maps_each_inner_tool() -> None:
    assert tool_namespaces(_codex_body()) == {
        "exec_shell": "kathara",
        "run_pingmesh_snapshot": "pingmesh",
    }


def test_restore_tool_namespaces_tags_only_flattened_calls() -> None:
    namespaces = {"exec_shell": "kathara"}
    item = {"type": "function_call", "name": "exec_shell", "arguments": "{}"}
    builtin = {"type": "function_call", "name": "exec_command", "arguments": "{}"}
    event = {"type": "response.completed", "response": {"output": [item, builtin]}}
    assert restore_tool_namespaces(event, namespaces)
    assert item["namespace"] == "kathara"
    assert "namespace" not in builtin
    assert not restore_tool_namespaces(
        {"type": "response.output_text.delta"}, namespaces
    )


def test_restore_stream_rewrites_events_split_across_chunks() -> None:
    call = {"type": "function_call", "name": "exec_shell", "arguments": "{}"}
    event = {"type": "response.output_item.done", "item": call}
    raw = (
        b'event: response.output_text.delta\ndata: {"delta": "hi"}\n\n'
        + b"event: response.output_item.done\ndata: "
        + json.dumps(event).encode()
        + b"\n\n"
    )

    async def chunks():
        for start in range(0, len(raw), 7):
            yield raw[start : start + 7]

    async def collect() -> bytes:
        return b"".join(
            [part async for part in _restore_stream(chunks(), {"exec_shell": "k"})]
        )

    out = asyncio.run(collect())
    events = [block for block in out.split(b"\n\n") if block]
    assert events[0] == b'event: response.output_text.delta\ndata: {"delta": "hi"}'
    data = json.loads(events[1].split(b"data: ", 1)[1])
    assert data["item"]["namespace"] == "k"
