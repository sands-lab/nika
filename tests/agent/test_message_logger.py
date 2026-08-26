"""Unit tests for messages.jsonl tool event logging."""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import ToolMessage

from agent.protocols import DIAGNOSIS
from agent.utils.loggers import (
    AgentCallbackLogger,
    MessageLogger,
    PendingToolCallTracker,
    tool_event_payload,
)


def test_tool_end_includes_tool_name_from_kwargs(tmp_path: Path) -> None:
    logger = AgentCallbackLogger(phase=DIAGNOSIS, session_dir=str(tmp_path))
    logger.on_tool_start(
        {"name": "get_reachability"},
        "{}",
        tool_call_id="call-1",
        run_id="00000000-0000-0000-0000-000000000001",
    )
    logger.on_tool_end(
        ToolMessage(content="ok", tool_call_id="call-1", name="get_reachability"),
        name="get_reachability",
        tool_call_id="call-1",
        run_id="00000000-0000-0000-0000-000000000002",
    )

    lines = (
        (tmp_path / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    entry = json.loads(lines[-1])
    assert entry["event"] == "tool_end"
    assert entry["tool"] == {"name": "get_reachability"}
    assert entry["tool_call_id"] == "call-1"
    assert entry["input"] == "{}"


def test_tool_end_includes_tool_name_from_tool_message(tmp_path: Path) -> None:
    logger = AgentCallbackLogger(phase=DIAGNOSIS, session_dir=str(tmp_path))
    logger.on_tool_start(
        {"name": "ping_pair"},
        '{"host_a": "pc1", "host_b": "pc2"}',
        tool_call_id="call-2",
        run_id="00000000-0000-0000-0000-000000000003",
    )
    logger.on_tool_end(
        ToolMessage(content="ok", tool_call_id="call-2", name="ping_pair"),
        run_id="00000000-0000-0000-0000-000000000004",
    )

    entry = json.loads(
        (tmp_path / "messages.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()[-1]
    )
    assert entry["event"] == "tool_end"
    assert entry["tool"] == {"name": "ping_pair"}
    assert entry["tool_call_id"] == "call-2"
    assert entry["input"] == '{"host_a": "pc1", "host_b": "pc2"}'


def test_pending_tool_call_tracker_pairs_same_name_calls() -> None:
    tracker = PendingToolCallTracker()
    tracker.register(
        name="frr_get_bgp_conf",
        input={"router_name": "router1"},
        tool_call_id="a",
    )
    tracker.register(
        name="frr_get_bgp_conf",
        input={"router_name": "router2"},
        tool_call_id="b",
    )
    first = tracker.resolve(name="frr_get_bgp_conf", tool_call_id="a")
    second = tracker.resolve(name="frr_get_bgp_conf", tool_call_id="b")
    assert first["input"] == '{"router_name": "router1"}'
    assert second["input"] == '{"router_name": "router2"}'


def test_tool_event_payload_normalizes_input() -> None:
    payload = tool_event_payload(
        name="exec_shell",
        input={"host_name": "pc1", "command": "hostname"},
        tool_call_id="call-3",
    )
    assert payload["tool"] == {"name": "exec_shell"}
    assert payload["tool_call_id"] == "call-3"
    assert '"command": "hostname"' in payload["input"]


def test_mcp_agent_post_tool_call_includes_correlation_fields(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, patch

    from mcp.types import (
        CallToolRequest,
        CallToolRequestParams,
        CallToolResult,
        TextContent,
    )

    from agent.byo.mcp_agent.llm import NikaOpenAIAugmentedLLM

    nika_logger = MessageLogger(phase=DIAGNOSIS, session_dir=str(tmp_path))
    llm = NikaOpenAIAugmentedLLM.__new__(NikaOpenAIAugmentedLLM)
    llm._nika_logger = nika_logger  # noqa: SLF001
    llm._pending_tool_calls = PendingToolCallTracker()

    request = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(
            name="frr_get_bgp_conf", arguments={"router_name": "router1"}
        ),
    )
    result = CallToolResult(content=[TextContent(type="text", text="ok")])

    with patch(
        "mcp_agent.workflows.llm.augmented_llm_openai.OpenAIAugmentedLLM.post_tool_call",
        new_callable=AsyncMock,
        return_value=result,
    ):
        import asyncio

        asyncio.run(llm.pre_tool_call("call-3", request))
        asyncio.run(llm.post_tool_call("call-3", request, result))

    lines = (
        (tmp_path / "messages.jsonl").read_text(encoding="utf-8").strip().splitlines()
    )
    start = json.loads(lines[0])
    end = json.loads(lines[1])
    assert start["event"] == "tool_start"
    assert end["event"] == "tool_end"
    assert start["tool_call_id"] == "call-3"
    assert end["tool_call_id"] == "call-3"
    assert start["input"] == '{"router_name": "router1"}'
    assert end["input"] == '{"router_name": "router1"}'
