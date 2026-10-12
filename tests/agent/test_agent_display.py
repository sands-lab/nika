"""User-visible conversation output and quiet-mode regression tests."""

import asyncio
import json

import pytest

from agent.utils.display import agent_output, display_agent_event
from agent.utils.loggers import MessageLogger

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("enabled", [True, False])
def test_display_does_not_change_trajectory(tmp_path, capsys, enabled):
    logger = MessageLogger("diagnosis", str(tmp_path))
    events = [
        ("llm_end", {"text": "BGP peer is down."}),
        ("tool_start", {"tool": {"name": "show_bgp"}, "input": "{}"}),
        ("tool_end", {"tool": {"name": "show_bgp"}, "output": "Idle"}),
        ("tool_error", {"tool": {"name": "ping"}, "error": "unreachable"}),
    ]
    with agent_output(enabled):
        for event, payload in events:
            logger.log(event, payload)
    output = capsys.readouterr().err
    if enabled:
        assert "💬 Agent:\nBGP peer is down." in output
        assert "🔧 Tool show_bgp: {}" in output
        assert "✓ Tool show_bgp\nIdle" in output
        assert "✗ Tool ping\nunreachable" in output
    else:
        assert output == ""
    entries = [
        json.loads(line)
        for line in (tmp_path / "messages.jsonl").read_text().splitlines()
    ]
    assert [
        (
            entry.pop("event"),
            {
                key: value
                for key, value in entry.items()
                if key not in {"timestamp", "phase"}
            },
        )
        for entry in entries
    ] == events


def test_native_claude_and_canonical_tools_display_identically(capsys):
    with agent_output(True):
        display_agent_event(
            "assistant",
            {
                "claude_event": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "mcp__server__ping",
                                "input": {"host": "pc1"},
                            }
                        ]
                    },
                }
            },
        )
        display_agent_event(
            "user",
            {
                "claude_event": {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call-1",
                                "content": [{"type": "text", "text": "reachable"}],
                            }
                        ]
                    },
                }
            },
        )
    native = capsys.readouterr().err
    with agent_output(True):
        display_agent_event(
            "tool_start", {"tool": {"name": "ping"}, "input": {"host": "pc1"}}
        )
        display_agent_event(
            "tool_end", {"tool": {"name": "ping"}, "output": "reachable"}
        )
    assert native == capsys.readouterr().err


def test_streamed_message_is_shown_once(capsys):
    with agent_output(True):
        display_agent_event(
            "item.completed",
            {
                "codex_event": {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Checking routes."},
                }
            },
        )
        assert "Checking routes." in capsys.readouterr().err
        display_agent_event("llm_end", {"text": "Checking routes."})
        assert capsys.readouterr().err == ""
    display_agent_event("llm_end", {"text": "Outside the run"})
    assert capsys.readouterr().err == ""


def test_concurrent_quiet_run_does_not_inherit_output(capsys):
    async def run(enabled, text):
        with agent_output(enabled):
            await asyncio.sleep(0)
            display_agent_event("llm_end", {"text": text})

    async def both():
        await asyncio.gather(run(True, "visible"), run(False, "quiet"))

    asyncio.run(both())
    output = capsys.readouterr().err
    assert "visible" in output
    assert "quiet" not in output


def test_serialized_mcp_content_is_readable(capsys):
    with agent_output(True):
        display_agent_event(
            "tool_end",
            {
                "tool": {"name": "exec_shell"},
                "output": "[{'type': 'text', 'text': 'router1\\nreachable', 'id': 'internal-id'}]",
            },
        )
    assert capsys.readouterr().err == "✓ Tool exec_shell\nrouter1\nreachable\n"


def test_model_text_and_tool_arguments_are_not_content_blocks(capsys):
    blocks = [{"type": "text", "text": "literal input"}]
    literal = repr(blocks)
    with agent_output(True):
        display_agent_event("llm_end", {"text": literal})
        display_agent_event("tool_start", {"tool": {"name": "submit"}, "input": blocks})
    output = capsys.readouterr().err
    assert f"💬 Agent:\n{literal}" in output
    assert f"🔧 Tool submit: {json.dumps(blocks)}" in output


def test_json_tool_result_includes_embedded_resources(capsys):
    output = json.dumps(
        [
            {"type": "text", "text": "reachable"},
            {"type": "resource", "resource": {"text": "route installed"}},
        ]
    )
    with agent_output(True):
        display_agent_event(
            "tool_end", {"tool": {"name": "show_route"}, "output": output}
        )
    assert capsys.readouterr().err == "✓ Tool show_route\nreachable\nroute installed\n"
