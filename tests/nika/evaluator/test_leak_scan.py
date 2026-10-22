"""Regression check: agent-visible text must not identify the benchmark."""

from __future__ import annotations

import json

import pytest

from nika.evaluator.leak_scan import scan_tool_metadata, scan_trajectory

pytestmark = pytest.mark.unit


def _write(path, events) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def test_scan_reports_identifying_text_from_each_agent_visible_source(
    tmp_path,
) -> None:
    trace = tmp_path / "messages.jsonl"
    _write(
        trace,
        [
            {"phase": "diagnosis", "event": "prompt", "text": "Lab run by NIKA."},
            {
                "phase": "diagnosis",
                "event": "system",
                "claude_event": {
                    "type": "system",
                    "subtype": "init",
                    "cwd": "/srv/nika/runtime/agent_workspaces/x",
                },
            },
            {
                "phase": "diagnosis",
                "event": "tool_end",
                "input": '{"command": "ls /etc/dhcp"}',
                "output": "dhclient-enter-hooks.d/nika-incorrect-dns",
            },
            {
                "phase": "diagnosis",
                "event": "user",
                "claude_event": {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": [{"type": "text", "text": "fault_drop"}],
                            }
                        ]
                    },
                },
            },
            {
                "phase": "submission",
                "event": "tool_end",
                "input": "{}",
                "output": "the injected delay; ground truth",
            },
        ],
    )

    hits = scan_trajectory(trace)

    assert {(hit.source, hit.term) for hit in hits} == {
        ("prompt", "nika"),
        ("session_metadata", "nika"),
        ("tool_output", "nika"),
        ("tool_output", "fault"),
        ("tool_output", "inject"),
        ("tool_output", "ground truth"),
    }


def test_scan_allows_ontology_wording_and_agent_written_text(tmp_path) -> None:
    trace = tmp_path / "messages.jsonl"
    report = "The faulty router injects nothing; NIKA-like naming."
    _write(
        trace,
        [
            {
                "phase": "diagnosis",
                "event": "prompt",
                "text": "Focus on (2) localizing the faulty devices. Use defaults.",
            },
            {
                "phase": "diagnosis",
                "event": "assistant",
                "claude_event": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "input": {"command": "echo faulty-check"},
                            },
                            {"type": "text", "text": "Looking for a fault."},
                        ]
                    },
                },
            },
            {
                "phase": "diagnosis",
                "event": "user",
                "claude_event": {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "faulty-check",
                            }
                        ]
                    },
                },
            },
            {"phase": "diagnosis", "event": "diagnosis_frozen", "report": report},
            {
                "phase": "submission",
                "event": "prompt",
                "text": f"Report:\n{report}\nfault ontology: link_down (fault_type)",
            },
        ],
    )

    assert scan_trajectory(trace) == []


def test_leaderboard_validate_warns_about_leaks_in_sibling_trajectories(
    tmp_path,
) -> None:
    from nika.workflows.leaderboard.validate import _trajectory_leak_warnings
    from nika.workflows.leaderboard.validate_trajectories import (
        sibling_trajectories_dir,
    )

    scores = tmp_path / "20261018_harness_model"
    scores.mkdir()
    trial = sibling_trajectories_dir(scores) / "trials" / "case__t01"
    trial.mkdir(parents=True)
    _write(
        trial / "messages.jsonl",
        [{"phase": "diagnosis", "event": "tool_end", "output": "/opt/nika/x"}],
    )

    (warning,) = _trajectory_leak_warnings(scores)
    assert "'nika' in tool_output: 1" in warning


def test_mcp_tool_metadata_does_not_identify_the_benchmark() -> None:
    """Tool names, descriptions, and schemas of every diagnosis server."""
    assert [hit.format() for hit in scan_tool_metadata()] == []
