"""One step list for a recorded trial, whatever agent harness wrote it.

Agent harnesses record ``messages.jsonl`` differently: Claude Code wraps its
stream-json events in ``claude_event``, Codex in ``codex_event`` plus
``tool_start``/``tool_end`` rows, and LangGraph agents write ``llm_end`` and
``tool_start``/``tool_end`` rows. :func:`load_trial` reads any of them into
numbered :class:`Step` records, so audit checks and reviewer packets cite
steps the same way for every submission.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from nika.evaluator.validity import _as_dict

StepKind = Literal[
    "prompt",
    "init",
    "agent_text",
    "reasoning",
    "tool_call",
    "tool_result",
    "harness",
]

MESSAGES_FILE = "messages.jsonl"
NIKA_LOG_FILE = "nika.jsonl"


@dataclass
class Step:
    """One agent-visible or agent-written event of a trial.

    ``index`` numbers steps from 0 in trace order; ``line`` is the 1-based
    line of ``messages.jsonl`` the step came from. ``tool`` is the bare tool
    name (``exec_shell``) and ``server`` the MCP server, when known.
    """

    index: int
    line: int
    phase: str
    kind: StepKind
    text: str = ""
    tool: str = ""
    server: str = ""
    call_id: str = ""
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "index": self.index,
            "line": self.line,
            "phase": self.phase,
            "kind": self.kind,
        }
        for key in ("tool", "server", "call_id"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.is_error:
            out["is_error"] = True
        out["text"] = self.text
        if self.meta:
            out["meta"] = self.meta
        return out


@dataclass
class Trial:
    """A trial directory with its parsed steps and side files."""

    trial_id: str
    path: Path
    steps: list[Step]
    run: dict[str, Any]
    submission: dict[str, Any]
    ground_truth: dict[str, Any]
    eval_metrics: dict[str, Any]
    nika_events: list[dict[str, Any]]
    bad_lines: int = 0

    @property
    def case_key(self) -> str:
        return str(self.run.get("case_key") or "")

    @property
    def scenario(self) -> str:
        fingerprint = _fingerprint(self.run)
        return str(fingerprint.get("scenario") or self.run.get("scenario") or "")

    @property
    def problems(self) -> list[str]:
        fingerprint = _fingerprint(self.run)
        problems = fingerprint.get("problems") or []
        return [str(p) for p in problems] if isinstance(problems, list) else []

    @property
    def is_healthy_case(self) -> bool:
        return not self.ground_truth.get("root_causes")

    def calls(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "tool_call"]


def _fingerprint(run: dict[str, Any]) -> dict[str, Any]:
    raw = run.get("benchmark_fingerprint")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def text_of(value: Any) -> str:
    """Flatten message content (strings, block lists, dicts) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_of(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return text_of(value["content"])
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def split_tool_name(name: str) -> tuple[str, str]:
    """``mcp__server__tool`` → ``(server, tool)``; other names → ``("", name)``."""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
    return "", name


def _input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class _Builder:
    def __init__(self) -> None:
        self.steps: list[Step] = []

    def add(self, line: int, phase: str, kind: StepKind, **kwargs: Any) -> None:
        self.steps.append(Step(len(self.steps), line, phase, kind, **kwargs))


def _claude_rows(b: _Builder, line: int, phase: str, event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        keys = (
            "cwd",
            "tools",
            "mcp_servers",
            "model",
            "permissionMode",
            "slash_commands",
            "skills",
            "agents",
            "plugins",
            "memory_paths",
            "claude_code_version",
            "apiKeySource",
            "output_style",
        )
        meta = {k: event[k] for k in keys if k in event}
        b.add(line, phase, "init", text=json.dumps(meta, ensure_ascii=False), meta=meta)
        return
    blocks = (event.get("message") or {}).get("content")
    if isinstance(blocks, str):
        blocks = [{"type": "text", "text": blocks}]
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if kind == "assistant" and btype == "text":
            b.add(line, phase, "agent_text", text=str(block.get("text") or ""))
        elif kind == "assistant" and btype == "thinking":
            b.add(line, phase, "reasoning", text=str(block.get("thinking") or ""))
        elif kind == "assistant" and btype == "tool_use":
            server, tool = split_tool_name(str(block.get("name") or ""))
            b.add(
                line,
                phase,
                "tool_call",
                text=_input_text(block.get("input")),
                tool=tool,
                server=server,
                call_id=str(block.get("id") or ""),
            )
        elif kind == "user" and btype == "tool_result":
            b.add(
                line,
                phase,
                "tool_result",
                text=text_of(block.get("content")),
                call_id=str(block.get("tool_use_id") or ""),
                is_error=bool(block.get("is_error")),
            )
    if kind == "result":
        meta = {
            k: event.get(k)
            for k in ("stop_reason", "subtype", "is_error", "num_turns")
            if k in event
        }
        b.add(line, phase, "harness", text=json.dumps(meta), meta=meta)


def _codex_item(b: _Builder, line: int, phase: str, item: dict[str, Any]) -> None:
    itype = item.get("type")
    if itype == "agent_message":
        if str(item.get("text") or "").strip():
            b.add(line, phase, "agent_text", text=str(item.get("text")))
    elif itype == "reasoning":
        b.add(line, phase, "reasoning", text=str(item.get("text") or ""))
    elif itype == "error":
        b.add(
            line, phase, "harness", text=str(item.get("message") or ""), is_error=True
        )
    elif itype == "command_execution":
        call_id = str(item.get("id") or "")
        b.add(
            line,
            phase,
            "tool_call",
            text=str(item.get("command") or ""),
            tool="local_shell",
            call_id=call_id,
        )
        b.add(
            line,
            phase,
            "tool_result",
            text=str(item.get("aggregated_output") or ""),
            call_id=call_id,
            is_error=item.get("exit_code") not in (0, None),
        )


def parse_messages(path: Path) -> tuple[list[Step], int]:
    """Parse one ``messages.jsonl`` into steps; also count unreadable lines."""
    b = _Builder()
    bad = 0
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    with handle:
        for line_no, raw in enumerate(handle, start=1):
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(row, dict):
                continue
            phase = str(row.get("phase") or "")
            event = row.get("event")
            if event == "prompt":
                b.add(line_no, phase, "prompt", text=text_of(row.get("text")))
            elif event == "mcp_config":
                servers = row.get("servers") or []
                b.add(
                    line_no,
                    phase,
                    "harness",
                    text=json.dumps({"mcp_config": servers}),
                    meta={"mcp_config": servers},
                )
            elif isinstance(row.get("claude_event"), dict):
                _claude_rows(b, line_no, phase, row["claude_event"])
            elif event == "item.completed" and isinstance(row.get("codex_event"), dict):
                _codex_item(b, line_no, phase, row["codex_event"].get("item") or {})
            elif event == "tool_start":
                server, tool = split_tool_name(str((row.get("tool") or {}).get("name")))
                b.add(
                    line_no,
                    phase,
                    "tool_call",
                    text=_input_text(row.get("input")),
                    tool=tool,
                    server=server,
                    call_id=str(row.get("tool_call_id") or ""),
                )
            elif event in ("tool_end", "tool_error"):
                # Failed calls end with ``tool_error`` instead of ``tool_end``.
                output = text_of(row.get("output"))
                b.add(
                    line_no,
                    phase,
                    "tool_result",
                    text=output,
                    call_id=str(row.get("tool_call_id") or ""),
                    is_error=event == "tool_error"
                    or str(row.get("output_type") or "") == "error",
                )
            elif event == "llm_end":
                reasoning = str(row.get("reasoning_content") or "")
                if reasoning.strip():
                    b.add(line_no, phase, "reasoning", text=reasoning)
                text = str(row.get("text") or "")
                if text.strip():
                    b.add(line_no, phase, "agent_text", text=text)
                info = _as_dict(row.get("generation_info"))
                if info.get("finish_reason") in ("length", "max_tokens"):
                    b.add(
                        line_no,
                        phase,
                        "harness",
                        text="finish_reason=" + str(info.get("finish_reason")),
                        meta={"finish_reason": info.get("finish_reason")},
                    )
            elif event == "max_steps_reached":
                b.add(line_no, phase, "harness", text="max_steps_reached")
    return b.steps, bad


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for raw in handle:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def load_trial(path: Path) -> Trial:
    """Read one trial directory of a trajectory package or benchmark result."""
    path = Path(path)
    steps, bad = parse_messages(path / MESSAGES_FILE)
    return Trial(
        trial_id=path.name,
        path=path,
        steps=steps,
        run=_read_json(path / "run.json"),
        submission=_read_json(path / "submission.json"),
        ground_truth=_read_json(path / "ground_truth.json"),
        eval_metrics=_read_json(path / "eval_metrics.json"),
        nika_events=_read_jsonl(path / NIKA_LOG_FILE),
        bad_lines=bad,
    )


def trial_dirs(root: Path) -> list[Path]:
    """Trial directories under a trajectory package, job dir, or single trial."""
    root = Path(root)
    if (root / MESSAGES_FILE).is_file() or (root / "run.json").is_file():
        if not (root / "trials").is_dir():
            return [root]
    base = root / "trials" if (root / "trials").is_dir() else root
    return sorted(
        p
        for p in base.iterdir()
        if p.is_dir() and ((p / MESSAGES_FILE).is_file() or (p / "run.json").is_file())
    )
