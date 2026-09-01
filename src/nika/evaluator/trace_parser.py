"""Parse messages.jsonl to extract agent trace metrics.

The parser reads the session message trace and optionally filters
by the ``phase`` field. Token counts, step counts, and timing are derived
from the diagnosis phase by default (``phase_filter=DIAGNOSIS``).
"""

import json
from datetime import datetime

from agent.protocols import DIAGNOSIS
from agent.utils.usage import normalize_usage


class AgentTraceParser:
    def __init__(self, trace_path: str, phase_filter: str | None = DIAGNOSIS) -> None:
        self.trace_path = trace_path
        self.phase_filter = phase_filter
        self.in_tokens = 0
        self.out_tokens = 0
        self.steps = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.time_taken = 0

    def _add_usage(self, usage: dict | None) -> None:
        tokens = normalize_usage(usage)
        self.in_tokens += tokens["input_tokens"]
        self.out_tokens += tokens["output_tokens"]

    def _record_event(self, entry: dict) -> None:
        event = entry.get("event")
        if event == "tool_start":
            self.tool_calls += 1
        elif event == "tool_error":
            self.tool_errors += 1
        elif event == "llm_end":
            self.steps += 1
            self._add_usage(entry.get("usage_metadata"))
        elif event == "item.started":
            codex_item = (entry.get("codex_event") or {}).get("item") or {}
            if codex_item.get("type") == "mcp_tool_call":
                self.tool_calls += 1
        elif event == "item.completed":
            codex_item = (entry.get("codex_event") or {}).get("item") or {}
            if (
                codex_item.get("type") == "mcp_tool_call"
                and codex_item.get("status") == "failed"
            ):
                self.tool_errors += 1
        elif event == "turn.completed":
            self.steps += 1
            self._add_usage((entry.get("codex_event") or {}).get("usage"))
        elif event == "assistant":
            # Claude Code stream-json: tool calls appear in message content blocks.
            content = ((entry.get("claude_event") or {}).get("message") or {}).get(
                "content"
            ) or []
            self.tool_calls += sum(
                1
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_use"
            )
        elif event == "result":
            # Claude Code stream-json: one result event per phase. ``num_turns``
            # is the agent loop count (same unit as ``max_steps`` / ``llm_end``).
            claude_event = entry.get("claude_event") or {}
            if not claude_event.get("is_error"):
                num_turns = claude_event.get("num_turns")
                self.steps += int(num_turns) if num_turns is not None else 1
                self._add_usage(claude_event.get("usage"))

    def parse_trace(self) -> dict:
        time_start: datetime | None = None
        time_end: datetime | None = None

        with open(self.trace_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if self.phase_filter and entry.get("phase") != self.phase_filter:
                    continue

                raw_ts = entry.get("timestamp")
                if raw_ts:
                    cur_time = datetime.fromisoformat(raw_ts)
                    if time_start is None or cur_time < time_start:
                        time_start = cur_time
                    if time_end is None or cur_time > time_end:
                        time_end = cur_time

                self._record_event(entry)

        self.time_taken = (
            (time_end - time_start).total_seconds() if time_start and time_end else 0
        )
        return {
            "in_tokens": self.in_tokens,
            "out_tokens": self.out_tokens,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "time_taken": self.time_taken,
        }
