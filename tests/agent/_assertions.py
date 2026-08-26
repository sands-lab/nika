from __future__ import annotations

import json
from pathlib import Path

from agent.protocols import DIAGNOSIS, SUBMISSION


def assert_phase_messages(
    messages: list[dict],
    *,
    require_diagnosis_tools: bool = True,
    require_submission_tools: bool = True,
) -> None:
    phases = {e["phase"] for e in messages}
    assert DIAGNOSIS in phases
    if require_submission_tools:
        assert SUBMISSION in phases

    if require_diagnosis_tools:
        diag_tools = [
            name
            for e in messages
            if e["phase"] == DIAGNOSIS
            for name in _extract_tool_names(e)
        ]
        assert diag_tools, "diagnosis phase must call at least one MCP tool"

    if require_submission_tools:
        sub_tools = [
            name
            for e in messages
            if e["phase"] == SUBMISSION
            for name in _extract_tool_names(e)
        ]
        assert any("submit" in name for name in sub_tools), sub_tools


def assert_submission_fields(session_dir: Path) -> None:
    submission = json.loads((session_dir / "submission.json").read_text())
    assert "is_anomaly" in submission
    assert isinstance(submission.get("root_causes"), list)
    for item in submission["root_causes"]:
        assert item.get("resource_id") or (item.get("resource") or {}).get("id")
        assert item.get("fault_type")


def _extract_tool_names(entry: dict) -> list[str]:
    names: list[str] = []
    if entry.get("event") == "tool_start" and "tool" in entry:
        names.append(str(entry["tool"].get("name", "")))

    claude_event = entry.get("claude_event")
    if isinstance(claude_event, dict) and claude_event.get("type") == "assistant":
        message = claude_event.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names.append(str(block.get("name", "")))

    codex_event = entry.get("codex_event")
    if isinstance(codex_event, dict):
        item = codex_event.get("item") or {}
        if item.get("type") == "mcp_tool_call":
            names.append(str(item.get("tool", "")))

    return names


def _message_text(entry: dict) -> str:
    parts: list[str] = []
    if entry.get("event") == "llm_end":
        parts.append(str(entry.get("text", "")))
    if entry.get("event") == "tool_start":
        parts.append(str(entry.get("input", "")))
    claude_event = entry.get("claude_event")
    if isinstance(claude_event, dict):
        parts.append(json.dumps(claude_event, ensure_ascii=False))
    codex_event = entry.get("codex_event")
    if isinstance(codex_event, dict):
        parts.append(json.dumps(codex_event, ensure_ascii=False))
    return "\n".join(parts)


def skill_invoked(messages: list[dict], skill_name: str = "nika-test-skill") -> bool:
    """Return True if messages show the named skill was invoked."""
    skill_markers = (
        skill_name,
        f"${skill_name}",
        "Launching skill:",
    )
    for entry in messages:
        if entry.get("event") == "tool_start":
            tool = entry.get("tool") or {}
            if tool.get("name") == "Skill" and skill_name in str(
                entry.get("input", "")
            ):
                return True
        claude_event = entry.get("claude_event")
        if isinstance(claude_event, dict) and claude_event.get("type") == "assistant":
            message = claude_event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                if block.get("name") == "Skill" and skill_name in json.dumps(
                    block.get("input") or {}, ensure_ascii=False
                ):
                    return True
        text = _message_text(entry)
        if any(marker in text for marker in skill_markers):
            return True
    return False


def marker_before_first_mcp_tool(
    messages: list[dict],
    marker: str = "NIKA_TEST_SKILL_ACTIVE",
) -> bool:
    """Return True when marker text appears before the first non-Skill MCP tool call."""
    for entry in messages:
        text = _message_text(entry)
        if marker in text:
            return True
        for tool_name in _extract_tool_names(entry):
            if tool_name and tool_name != "Skill":
                return False
    return False


def assert_skill_invoked(
    messages: list[dict],
    skill_name: str = "nika-test-skill",
) -> None:
    invoked = skill_invoked(messages, skill_name=skill_name)
    workflow = marker_before_first_mcp_tool(messages)
    assert invoked or workflow, (
        f"expected skill {skill_name!r} to be invoked or its marker-first workflow followed"
    )
    assert workflow, "expected NIKA_TEST_SKILL_ACTIVE before the first MCP tool call"


def reachability_called_before_submit(messages: list[dict]) -> bool:
    saw_reachability = False
    for entry in messages:
        for tool_name in _extract_tool_names(entry):
            if "submit" in tool_name:
                return saw_reachability
            if "get_reachability" in tool_name:
                saw_reachability = True
    return saw_reachability
