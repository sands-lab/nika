from __future__ import annotations

import json
from pathlib import Path

from nika.evaluator.trace_parser import AgentTraceParser


def _write_trace(tmp_path: Path, entries: list[dict]) -> str:
    path = tmp_path / "messages.jsonl"
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    return str(path)


class TraceParserTest:
    def test_claude_result_uses_num_turns_and_cache_tokens(
        self, tmp_path: Path
    ) -> None:
        path = _write_trace(
            tmp_path,
            [
                {
                    "phase": "diagnosis",
                    "event": "assistant",
                    "claude_event": {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "tool_use", "name": "ping_pair"},
                                {"type": "tool_use", "name": "frr_show_ip_route"},
                            ]
                        },
                    },
                },
                {
                    "phase": "diagnosis",
                    "event": "result",
                    "claude_event": {
                        "is_error": False,
                        "num_turns": 41,
                        "usage": {
                            "input_tokens": 16262,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 107648,
                            "output_tokens": 11272,
                        },
                    },
                },
                {
                    "phase": "submission",
                    "event": "result",
                    "claude_event": {
                        "is_error": False,
                        "num_turns": 4,
                        "usage": {
                            "input_tokens": 100,
                            "cache_read_input_tokens": 50,
                            "output_tokens": 20,
                        },
                    },
                },
            ],
        )
        metrics = AgentTraceParser(trace_path=path).parse_trace()
        assert metrics["steps"] == 41
        assert metrics["in_tokens"] == 16262 + 107648
        assert metrics["out_tokens"] == 11272
        assert metrics["tool_calls"] == 2

    def test_claude_result_without_num_turns_counts_one_step(
        self, tmp_path: Path
    ) -> None:
        path = _write_trace(
            tmp_path,
            [
                {
                    "phase": "diagnosis",
                    "event": "result",
                    "claude_event": {
                        "is_error": False,
                        "usage": {"input_tokens": 10, "output_tokens": 4},
                    },
                }
            ],
        )
        metrics = AgentTraceParser(trace_path=path).parse_trace()
        assert metrics["steps"] == 1
        assert metrics["in_tokens"] == 10
        assert metrics["out_tokens"] == 4

    def test_llm_end_still_counts_each_completion(self, tmp_path: Path) -> None:
        path = _write_trace(
            tmp_path,
            [
                {
                    "phase": "diagnosis",
                    "event": "llm_end",
                    "usage_metadata": {"input_tokens": 11, "output_tokens": 3},
                },
                {
                    "phase": "diagnosis",
                    "event": "llm_end",
                    "usage_metadata": {"input_tokens": 9, "output_tokens": 2},
                },
                {"phase": "diagnosis", "event": "tool_start"},
            ],
        )
        metrics = AgentTraceParser(trace_path=path).parse_trace()
        assert metrics["steps"] == 2
        assert metrics["in_tokens"] == 20
        assert metrics["out_tokens"] == 5
        assert metrics["tool_calls"] == 1

    def test_codex_turn_completed_counts_each_turn(self, tmp_path: Path) -> None:
        path = _write_trace(
            tmp_path,
            [
                {
                    "phase": "diagnosis",
                    "event": "turn.completed",
                    "codex_event": {"usage": {"input_tokens": 8, "output_tokens": 1}},
                },
                {
                    "phase": "diagnosis",
                    "event": "turn.completed",
                    "codex_event": {"usage": {"input_tokens": 4, "output_tokens": 2}},
                },
            ],
        )
        metrics = AgentTraceParser(trace_path=path).parse_trace()
        assert metrics["steps"] == 2
        assert metrics["in_tokens"] == 12
        assert metrics["out_tokens"] == 3

    def test_langchain_cache_details_are_not_double_counted(
        self, tmp_path: Path
    ) -> None:
        path = _write_trace(
            tmp_path,
            [
                {
                    "phase": "diagnosis",
                    "event": "llm_end",
                    "usage_metadata": {
                        "input_tokens": 100,
                        "output_tokens": 8,
                        "input_token_details": {"cache_read": 80},
                    },
                }
            ],
        )
        metrics = AgentTraceParser(trace_path=path).parse_trace()
        assert metrics["in_tokens"] == 100
        assert metrics["out_tokens"] == 8

    def test_openai_prompt_token_aliases(self, tmp_path: Path) -> None:
        path = _write_trace(
            tmp_path,
            [
                {
                    "phase": "diagnosis",
                    "event": "llm_end",
                    "usage_metadata": {
                        "prompt_tokens": 40,
                        "completion_tokens": 6,
                    },
                }
            ],
        )
        metrics = AgentTraceParser(trace_path=path).parse_trace()
        assert metrics["in_tokens"] == 40
        assert metrics["out_tokens"] == 6
