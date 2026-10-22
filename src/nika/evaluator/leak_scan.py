"""Find benchmark-identifying text in what agents saw during a run.

An agent that can tell it runs inside NIKA, or that a fault was injected, can
go looking for the benchmark's answers. This scan reads recorded trajectories
(``messages.jsonl``) and reports the terms ``nika``, ``inject``, ``fault``,
and ``ground truth`` in agent-visible text: prompts, the session metadata the
agent CLI reports (working directory, tools, MCP servers, skills), and tool
outputs. The agent's own messages are not scanned.

Submission prompts and tools legitimately speak of the fault ontology and
``fault_type``, so ``fault`` is allowed in the submission phase. The shared
diagnosis task prompt asks to localize "the faulty devices"; that phrase is
allowed too. :func:`scan_tool_metadata` applies the same terms to the MCP
tool descriptions and parameter schemas agents receive.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.protocols import SUBMISSION
from agent.utils.loggers import MESSAGES_FILENAME

# Word starts only, so "default" is no hit; "nika_x" and "/nika-x" are.
TERM_PATTERN = re.compile(
    r"(?<![a-z])(?:nika|inject|fault|ground[\s_-]*truth)", re.IGNORECASE
)
# Wording of the shared diagnosis task prompt, identical for every agent.
_ALLOWED_PHRASES = re.compile(r"localizing the faulty devices", re.IGNORECASE)
# Session metadata fields of a Claude Code ``system``/``init`` event.
_SESSION_METADATA_KEYS = (
    "cwd",
    "tools",
    "mcp_servers",
    "slash_commands",
    "skills",
    "agents",
    "plugins",
    "memory_paths",
)
_EXCERPT_CHARS = 60


@dataclass(frozen=True)
class LeakHit:
    """One benchmark-identifying term in agent-visible text."""

    path: str
    line: int
    phase: str
    source: str
    term: str
    excerpt: str

    def format(self) -> str:
        return (
            f"{self.path}:{self.line} [{self.phase}/{self.source}] "
            f"{self.term!r}: …{self.excerpt}…"
        )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if "content" in value:
            return _text(value["content"])
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def agent_visible_texts(
    event: dict[str, Any], *, tool_inputs: dict[str, str] | None = None
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(source, text, authored)`` for the agent-visible parts of *event*.

    *authored* is text the agent wrote itself (the tool call's input), whose
    words echoed back in the output are not attributed to the environment.
    *tool_inputs* maps Claude tool-use ids to inputs seen earlier in the trace.
    """
    name = event.get("event")
    if name == "prompt":
        yield "prompt", _text(event.get("text")), ""
    elif name == "tool_end":
        yield "tool_output", _text(event.get("output")), _text(event.get("input"))
    claude = event.get("claude_event")
    if not isinstance(claude, dict):
        return
    content = (claude.get("message") or {}).get("content")
    blocks = [block for block in content or [] if isinstance(block, dict)]
    if claude.get("type") == "system" and claude.get("subtype") == "init":
        metadata = {key: claude[key] for key in _SESSION_METADATA_KEYS if key in claude}
        yield "session_metadata", json.dumps(metadata, ensure_ascii=False), ""
    elif claude.get("type") == "assistant" and tool_inputs is not None:
        for block in blocks:
            if block.get("type") == "tool_use":
                tool_inputs[str(block.get("id"))] = _text(block.get("input"))
    elif claude.get("type") == "user":
        for block in blocks:
            if block.get("type") == "tool_result":
                authored = (tool_inputs or {}).get(str(block.get("tool_use_id")), "")
                yield "tool_output", _text(block.get("content")), authored


def scan_text(
    text: str, *, phase: str, authored: str = ""
) -> Iterator[tuple[str, str]]:
    """Yield ``(term, excerpt)`` for each disallowed term in *text*.

    A hit whose word also occurs in *authored* (agent-written text) is skipped.
    """
    allowed = [match.span() for match in _ALLOWED_PHRASES.finditer(text)]
    authored = authored.lower()
    for match in TERM_PATTERN.finditer(text):
        term = re.sub(r"[\s_-]+", " ", match.group(0).lower())
        if phase == SUBMISSION and term == "fault":
            continue
        if any(start <= match.start() < end for start, end in allowed):
            continue
        word = re.match(r"[\w-]*", text[match.start() :]).group(0).lower()
        if authored and word in authored:
            continue
        start = max(match.start() - _EXCERPT_CHARS, 0)
        excerpt = text[start : match.end() + _EXCERPT_CHARS].replace("\n", " ")
        yield term, excerpt


def scan_trajectory(path: str | Path) -> list[LeakHit]:
    """Return the disallowed terms in agent-visible text of one ``messages.jsonl``.

    The submission prompt repeats the agent's own diagnosis report, which is
    left out.
    """
    hits: list[LeakHit] = []
    tool_inputs: dict[str, str] = {}
    report = ""
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "diagnosis_frozen":
                report = _text(event.get("report")).strip()
            phase = str(event.get("phase") or "")
            for source, text, authored in agent_visible_texts(
                event, tool_inputs=tool_inputs
            ):
                if source == "prompt" and report:
                    text = text.replace(report, "")
                for term, excerpt in scan_text(text, phase=phase, authored=authored):
                    hits.append(
                        LeakHit(str(path), line_no, phase, source, term, excerpt)
                    )
    return hits


def summarize_hits(hits: list[LeakHit], *, n_trajectories: int) -> str:
    """One line: hit counts by term and agent-visible source."""
    if not hits:
        return f"No benchmark-identifying text in {n_trajectories} trajectories."
    counts: dict[tuple[str, str], int] = {}
    for hit in hits:
        counts[(hit.term, hit.source)] = counts.get((hit.term, hit.source), 0) + 1
    paths = {hit.path for hit in hits if hit.source != "tool_metadata"}
    detail = ", ".join(
        f"{term!r} in {source}: {count}"
        for (term, source), count in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return (
        f"{len(hits)} benchmark-identifying hits ({len(paths)} of "
        f"{n_trajectories} trajectories): {detail}"
    )


def trajectory_files(paths: Iterable[str | Path]) -> list[Path]:
    """Expand files and directories into the ``messages.jsonl`` files they hold."""
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.rglob(MESSAGES_FILENAME)))
        elif path.is_file():
            files.append(path)
    return files


def scan_tool_metadata() -> list[LeakHit]:
    """Return disallowed terms in the MCP tool metadata diagnosis agents receive.

    Lists the tools of every diagnosis MCP server with the description note
    the gateway adds, and scans their names, descriptions, and input schemas.
    The submission server is skipped, since its tools describe the fault
    ontology by design.
    """
    import asyncio
    import importlib

    from nika.mcp.registry import MCP_SERVER_SPECS, SUBMISSION_SERVER
    from nika.mcp.tool_output import annotate_tools_list_result

    hits: list[LeakHit] = []
    for name, spec in sorted(MCP_SERVER_SPECS.items()):
        if name == SUBMISSION_SERVER:
            continue
        try:
            server = importlib.import_module(spec.module).mcp
        except ImportError:
            # Optional backends (for example Kubernetes) need extras.
            continue
        tools = [
            tool.model_dump(mode="json", exclude_none=True)
            for tool in asyncio.run(server.list_tools())
        ]
        for tool in annotate_tools_list_result({"tools": tools})["tools"]:
            text = json.dumps(tool, ensure_ascii=False)
            for term, excerpt in scan_text(text, phase="diagnosis"):
                hits.append(
                    LeakHit(name, 0, "diagnosis", "tool_metadata", term, excerpt)
                )
    return hits
