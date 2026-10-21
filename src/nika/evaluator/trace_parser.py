"""Extract agent trace metrics of a session.

The parser reads the session message trace (``messages.jsonl``) and optionally
filters by the ``phase`` field; metrics cover the diagnosis phase by default
(``phase_filter=DIAGNOSIS``). The agent writes that trace in its sandbox, so
the host's own records win when the session has them: steps and tokens come
from the model proxy (``model_calls.jsonl``, one step per LLM request) and tool
calls from the MCP gateway (``tool_calls.jsonl``). Timing stays with the trace.
"""

import json
from datetime import datetime
from pathlib import Path

from agent.protocols import DIAGNOSIS
from agent.utils.usage import normalize_usage
from nika.agents.sandbox.model_calls import MODEL_CALLS_FILENAME, model_call_metrics
from nika.mcp.gateway.tool_log import TOOL_CALLS_FILENAME


class AgentTraceParser:
    def __init__(self, trace_path: str, phase_filter: str | None = DIAGNOSIS) -> None:
        self.trace_path = trace_path
        self.phase_filter = phase_filter
        self.in_tokens = 0
        self.out_tokens = 0
        self.reasoning_tokens = 0
        self.steps = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.time_taken = 0

    def _add_usage(self, usage: dict | None) -> None:
        tokens = normalize_usage(usage)
        self.in_tokens += tokens["input_tokens"]
        self.out_tokens += tokens["output_tokens"]
        self.reasoning_tokens += tokens["reasoning_tokens"]

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
            # ``error_max_turns`` means the phase used its whole max_steps budget;
            # its turns and usage still count.
            claude_event = entry.get("claude_event") or {}
            if (
                not claude_event.get("is_error")
                or claude_event.get("subtype") == "error_max_turns"
            ):
                num_turns = claude_event.get("num_turns")
                self.steps += int(num_turns) if num_turns is not None else 1
                self._add_usage(claude_event.get("usage"))

    def _apply_host_records(self, session_dir: Path) -> None:
        """Replace counts with the host's records of the session, if any."""
        model_calls = session_dir / MODEL_CALLS_FILENAME
        if model_calls.is_file():
            metrics = model_call_metrics(model_calls, phase=self.phase_filter)
            self.steps = metrics["steps"]
            self.in_tokens = metrics["in_tokens"]
            self.out_tokens = metrics["out_tokens"]
            self.reasoning_tokens = metrics["reasoning_tokens"]
        tool_calls = session_dir / TOOL_CALLS_FILENAME
        if tool_calls.is_file():
            self.tool_calls = self.tool_errors = 0
            with tool_calls.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if self.phase_filter and entry.get("phase") != self.phase_filter:
                        continue
                    self.tool_calls += 1
                    self.tool_errors += int(bool(entry.get("is_error")))

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
                # Lab-host rows (remote mode) use another clock; time the agent only.
                if raw_ts and entry.get("origin") != "remote":
                    cur_time = datetime.fromisoformat(raw_ts)
                    if time_start is None or cur_time < time_start:
                        time_start = cur_time
                    if time_end is None or cur_time > time_end:
                        time_end = cur_time

                self._record_event(entry)

        self.time_taken = (
            (time_end - time_start).total_seconds() if time_start and time_end else 0
        )
        self._apply_host_records(Path(self.trace_path).parent)
        return {
            "in_tokens": self.in_tokens,
            "out_tokens": self.out_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "time_taken": self.time_taken,
        }
