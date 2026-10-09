"""Adapt messages.jsonl and nika.jsonl rows into CanonicalTraceEvent."""

from __future__ import annotations

import json
from collections.abc import Iterator
from itertools import groupby
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nika.inspect.models import CanonicalTraceEvent, EventKind, ToolPayload
from nika.utils.logger import event_summary

_LIFECYCLE_EVENTS = frozenset(
    {
        "env_start",
        "env_verify",
        "env_verify_progress",
        "env_ready",
        "failure_injected",
        "failure_verified",
        "failure_inject_complete",
        "failure_inject_error",
        "network_change",
        "traffic_start",
        "traffic_stop",
        # Session bookends (nika.jsonl): agent_start → agent_end | agent_error
        # Phase bookends (messages.jsonl): agent_start → agent_done | agent_error
        # Sandbox bookends (nika.jsonl): sandbox_start → sandbox_end
        "agent_start",
        "agent_done",
        "agent_end",
        "agent_error",
        "sandbox_start",
        "sandbox_end",
        "session_close",
        "session_closed",
        "lab_undeploy",
        "lab_undeployed",
        "mcp_attach",
        "mcp_detach",
    }
)

# Codex provider errors often echo the full chat history inside one string.
# Cap before the timeline API / PrettyValue try to render them.
_RAW_STR_LIMIT = 4_000
_RAW_LIST_LIMIT = 40
_RAW_DEPTH_LIMIT = 12


def _truncate(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
    # Avoid whitespace-collapsing multi-MB provider dumps.
    if len(text) > limit * 8:
        text = text[: limit * 8]
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _opt_text(value: Any) -> str | None:
    """Coerce a loosely typed log field to ``str | None`` (empty → ``None``)."""
    if value is None or value == "" or value == {} or value == []:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _timestamp(value: Any) -> str | None:
    """ISO timestamp from a string or epoch seconds; anything else → ``None``."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _unwrap_error_message(text: str) -> str:
    """Peel nested JSON ``{error:{message}}`` / ``{message}`` wrappers."""
    cur = text.strip()
    for _ in range(4):
        if len(cur) < 2 or cur[0] != "{":
            break
        try:
            obj = json.loads(cur)
        except json.JSONDecodeError:
            # Provider sometimes stores a Python-dict repr; keep as-is.
            break
        if not isinstance(obj, dict):
            break
        nested = obj.get("error")
        if isinstance(nested, dict) and nested.get("message") is not None:
            cur = str(nested["message"]).strip()
            continue
        if nested is not None and not isinstance(nested, dict):
            cur = str(nested).strip()
            continue
        if obj.get("message") is not None:
            cur = str(obj["message"]).strip()
            continue
        break
    return cur


def _summarize_error_blob(value: Any) -> str:
    """Short ledger line for provider errors that embed full chat transcripts."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _unwrap_error_message(text)
    # Drop echoed request payloads after the validation header / msg field.
    for marker in ("'input':", '"input":', ", 'input':", ', "input":'):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx].rstrip(" ,:{")
            break
    # Prefer the first non-empty lines (e.g. "197 validation errors:" + msg).
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        head = lines[0]
        if len(lines) > 1 and len(head) < 80:
            head = f"{head} {lines[1]}"
        text = head
    # Collapse pydantic-style dict noise to the human ``msg`` when present.
    for key in ("'msg': ", '"msg": '):
        idx = text.find(key)
        if idx < 0:
            continue
        rest = text[idx + len(key) :]
        quote = rest[:1]
        if quote not in {"'", '"'}:
            continue
        end = rest.find(quote, 1)
        if end > 1:
            msg = rest[1:end]
            prefix = lines[0] if lines and lines[0].endswith(":") else ""
            text = f"{prefix} {msg}".strip() if prefix else msg
            break
    return _truncate(text)


def _codex_error_text(entry: dict[str, Any]) -> str | None:
    """Pull human-readable text from Codex ``error`` / ``turn.failed`` events."""
    if entry.get("message"):
        return str(entry["message"])
    if entry.get("error") is not None:
        err = entry["error"]
        if isinstance(err, dict) and err.get("message") is not None:
            return str(err["message"])
        return str(err)
    codex = entry.get("codex_event")
    if not isinstance(codex, dict):
        return None
    if codex.get("message") is not None:
        return str(codex["message"])
    err = codex.get("error")
    if isinstance(err, dict) and err.get("message") is not None:
        return str(err["message"])
    if err is not None:
        return str(err)
    return None


def _summarize_subprocess_stderr(value: Any) -> str:
    """Prefer the first ERROR / error= line over Codex stdin chatter."""
    if value is None:
        return ""
    text = str(value)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        idx = stripped.find("error=")
        if idx >= 0:
            return _truncate(stripped[idx + len("error=") :].strip())
        lower = stripped.lower()
        if " error " in f" {lower} " or lower.startswith("error"):
            return _truncate(stripped)
    return _truncate(text)


def _slim_raw(value: Any, *, depth: int = 0) -> Any:
    """Truncate oversized strings/lists in event ``raw`` for the timeline API."""
    if depth > _RAW_DEPTH_LIMIT:
        return "… [nested value]"
    if isinstance(value, str):
        if len(value) <= _RAW_STR_LIMIT:
            return value
        return f"{value[:_RAW_STR_LIMIT]}… [{len(value)} chars total]"
    if isinstance(value, dict):
        return {str(k): _slim_raw(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > _RAW_LIST_LIMIT:
            head = [_slim_raw(v, depth=depth + 1) for v in value[:_RAW_LIST_LIMIT]]
            head.append(f"… [{len(value) - _RAW_LIST_LIMIT} more items]")
            return head
        return [_slim_raw(v, depth=depth + 1) for v in value]
    return value


def _raw_payload(entry: Any, *, slim: bool) -> tuple[dict[str, Any], bool]:
    """``(raw, truncated)`` for an event; ``slim=False`` keeps the full entry."""
    if not isinstance(entry, dict):
        return {}, False
    if not slim:
        return entry, False
    raw = _slim_raw(entry)
    return raw, raw != entry


def _maybe_json(value: Any) -> Any:
    """Parse JSON object/array strings; leave other values unchanged."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) < 2 or text[0] not in "{[" or text[-1] not in "}]":
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _codex_item(entry: dict[str, Any]) -> dict[str, Any] | None:
    item = (entry.get("codex_event") or {}).get("item")
    return item if isinstance(item, dict) else None


def _claude_event(entry: dict[str, Any]) -> dict[str, Any] | None:
    claude = entry.get("claude_event")
    return claude if isinstance(claude, dict) else None


def _claude_content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Content blocks from Claude stream-json ``message.content``."""
    claude = _claude_event(entry)
    if not claude:
        return []
    message = claude.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _first_content_block(
    blocks: list[dict[str, Any]], block_type: str
) -> dict[str, Any] | None:
    for block in blocks:
        if block.get("type") == block_type:
            return block
    return None


def _claude_text(blocks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "text" and block.get("text"):
            parts.append(str(block["text"]))
        elif block_type == "thinking" and block.get("thinking"):
            parts.append(str(block["thinking"]))
    return "\n".join(parts)


def _claude_tool_result_fields(
    entry: dict[str, Any],
) -> tuple[Any, str | None, bool] | None:
    """Parse Claude tool result: ``(output, tool_call_id, is_error)`` or None.

    Handles both structured ``message.content[]`` tool_result blocks and the
    alternate shape where ``message`` is a string and ``tool_use_result`` is set.
    """
    tool_result = _first_content_block(_claude_content_blocks(entry), "tool_result")
    if tool_result:
        call_id = tool_result.get("tool_use_id")
        return (
            tool_result.get("content"),
            str(call_id) if call_id is not None else None,
            bool(tool_result.get("is_error")),
        )

    claude = _claude_event(entry)
    if not claude:
        return None
    if claude.get("type") != "user" and entry.get("event") != "user":
        return None

    result = claude.get("tool_use_result")
    message = claude.get("message")
    if result is not None:
        output: Any = result
    elif isinstance(message, str) and message.strip():
        output = message
    else:
        return None

    call_id = claude.get("tool_use_id")
    if call_id is None:
        parent = claude.get("parent_tool_use_id")
        call_id = parent if parent not in (None, "") else None
    text = str(output)
    is_error = text.lower().startswith("error") or (
        isinstance(message, str) and message.lower().startswith("error")
    )
    return output, str(call_id) if call_id is not None else None, is_error


def _tool_name(entry: dict[str, Any]) -> str | None:
    tool = entry.get("tool")
    if isinstance(tool, dict) and tool.get("name"):
        return str(tool["name"])
    if entry.get("name"):
        return str(entry["name"])
    codex_item = _codex_item(entry)
    if codex_item:
        if codex_item.get("tool"):
            return str(codex_item["tool"])
        if codex_item.get("name"):
            return str(codex_item["name"])
    tool_use = _first_content_block(_claude_content_blocks(entry), "tool_use")
    if tool_use and tool_use.get("name"):
        return str(tool_use["name"])
    return None


def _tool_from_entry(entry: dict[str, Any], *, error: str | None = None) -> ToolPayload:
    tool = entry.get("tool") if isinstance(entry.get("tool"), dict) else {}
    name = _tool_name(entry)
    tool_call_id = entry.get("tool_call_id") or tool.get("id")
    codex_item = _codex_item(entry)
    input_value = entry.get("input", tool.get("input"))
    output_value = entry.get("output", tool.get("output"))
    if codex_item and codex_item.get("type") == "mcp_tool_call":
        input_value = (
            input_value
            if input_value is not None
            else (codex_item.get("arguments") or codex_item.get("input"))
        )
        output_value = (
            output_value
            if output_value is not None
            else (codex_item.get("result") or codex_item.get("output"))
        )
        if not tool_call_id and codex_item.get("id") is not None:
            tool_call_id = codex_item.get("id")
        if not error and codex_item.get("error") is not None:
            error = str(codex_item.get("error"))
    tool_use = _first_content_block(_claude_content_blocks(entry), "tool_use")
    if tool_use:
        if input_value is None:
            input_value = tool_use.get("input")
        if not tool_call_id and tool_use.get("id") is not None:
            tool_call_id = tool_use.get("id")
        if not name and tool_use.get("name"):
            name = str(tool_use["name"])
    result_fields = _claude_tool_result_fields(entry)
    if result_fields:
        result_output, result_call_id, is_error = result_fields
        if output_value is None:
            output_value = result_output
        if not tool_call_id and result_call_id:
            tool_call_id = result_call_id
        if not error and is_error:
            error = str(result_output) if result_output is not None else "tool error"
    return ToolPayload(
        name=name,
        input=_maybe_json(input_value),
        output=_maybe_json(output_value),
        tool_call_id=str(tool_call_id) if tool_call_id else None,
        error=error or _opt_text(entry.get("error")),
    )


def _agent_kind_and_title(entry: dict[str, Any]) -> tuple[EventKind, str, str]:
    event = str(entry.get("event") or "other")
    phase = entry.get("phase")
    item = _codex_item(entry)

    if event in {"item.started", "item.completed"} and item:
        item_type = str(item.get("type") or "")
        if item_type == "mcp_tool_call":
            name = str(item.get("tool") or item.get("name") or "tool")
            if event == "item.started":
                return (
                    "tool_call",
                    f"tool {name}",
                    _truncate(item.get("arguments") or item.get("input")),
                )
            if item.get("status") == "failed":
                return (
                    "tool_error",
                    f"tool error {name}",
                    _truncate(item.get("error")),
                )
            return (
                "tool_result",
                f"tool result {name}",
                _truncate(item.get("result") or item.get("output")),
            )
        if item_type == "agent_message":
            return "llm", "assistant", _truncate(item.get("text"))
        if item_type == "reasoning":
            # Folded into the surrounding turn as thinking by the viewer.
            return "llm", "reasoning", _truncate(item.get("text"))
        if item_type == "error":
            return (
                "system",
                "warning",
                _summarize_error_blob(item.get("message") or item.get("text")),
            )
        return "other", item_type or event, _truncate(item.get("text") or item)

    # Claude CLI stream-json: tool_use / tool_result / text live under claude_event.
    claude_blocks = _claude_content_blocks(entry)
    if claude_blocks:
        tool_use = _first_content_block(claude_blocks, "tool_use")
        if tool_use:
            name = str(tool_use.get("name") or "tool")
            return "tool_call", f"tool {name}", _truncate(tool_use.get("input"))
        tool_result = _first_content_block(claude_blocks, "tool_result")
        if tool_result:
            name = _tool_name(entry) or "tool"
            content = tool_result.get("content")
            if tool_result.get("is_error"):
                return "tool_error", f"tool error {name}", _truncate(content)
            return "tool_result", f"tool result {name}", _truncate(content)
        text = _claude_text(claude_blocks)
        if text or event == "assistant":
            return "llm", event if event != "other" else "assistant", _truncate(text)

    # Alternate Claude user/tool-result shape (string message + tool_use_result).
    result_fields = _claude_tool_result_fields(entry)
    if result_fields:
        output, _, is_error = result_fields
        name = _tool_name(entry) or "tool"
        if is_error:
            return "tool_error", f"tool error {name}", _truncate(output)
        return "tool_result", f"tool result {name}", _truncate(output)

    if event == "tool_start":
        name = _tool_name(entry) or "tool"
        return "tool_call", f"tool {name}", _truncate(entry.get("input"))
    if event == "tool_end":
        name = _tool_name(entry) or "tool"
        return "tool_result", f"tool result {name}", _truncate(entry.get("output"))
    if event in {"tool_error"}:
        name = _tool_name(entry) or "tool"
        return "tool_error", f"tool error {name}", _truncate(entry.get("error"))
    # Codex CLI turns — pair like llm_start / llm_end in the viewer.
    if event == "turn.started":
        return "llm", "turn", ""
    if event == "turn.completed":
        return (
            "llm",
            "turn completed",
            _truncate(entry.get("text") or entry.get("messages")),
        )
    if event == "turn.failed":
        return "llm", "turn failed", _summarize_error_blob(_codex_error_text(entry))
    if event in {"llm_start", "llm_end", "assistant"}:
        return "llm", event, _truncate(entry.get("text") or entry.get("messages"))
    if event == "llm_retry":
        # HTTP/provider retry inside one LangChain run (see ReasoningChatOpenAI).
        return "llm", "llm_retry", _summarize_error_blob(entry.get("error"))
    if event == "error":
        # Stream reconnect chatter stays system-level; fold into the turn in UI.
        return "system", "error", _summarize_error_blob(_codex_error_text(entry))
    if event == "llm_end_error":
        return (
            "llm",
            event,
            _summarize_error_blob(entry.get("error") or _codex_error_text(entry)),
        )
    if event == "agent_error":
        return (
            "other",
            event,
            _summarize_error_blob(entry.get("error") or _codex_error_text(entry)),
        )
    if event == "subprocess_error":
        return (
            "system",
            "subprocess_error",
            _summarize_subprocess_stderr(
                entry.get("stderr") or entry.get("error") or _codex_error_text(entry)
            ),
        )
    # Codex / CLI bookkeeping — keep off the Agent/Other flood.
    if event in {"mcp_config", "prompt", "subprocess_start", "thread.started"}:
        return (
            "system",
            event.replace(".", " "),
            _truncate(
                entry.get("command") or entry.get("servers") or entry.get("message")
            ),
        )
    # Phase bookends — same names across Codex / Claude / BYO / CLI.
    if event in {"agent_start", "agent_done"}:
        label = f"{event}" + (f" ({phase})" if phase else "")
        return "phase", label, _truncate(entry.get("message"))
    if event == "diagnosis_frozen":
        return "submission", "diagnosis frozen", _truncate(entry.get("report"))
    if event == "result":
        claude = _claude_event(entry) or {}
        return (
            "phase",
            "phase result",
            _truncate(claude.get("result")),
        )
    if event == "subprocess_nonzero_recovered":
        return (
            "other",
            event,
            _truncate(entry.get("stderr") or entry.get("message")),
        )
    return (
        "other",
        event,
        _truncate(
            entry.get("message") or entry.get("text") or _codex_error_text(entry)
        ),
    )


def adapt_agent_event(
    entry: dict[str, Any], *, index: int, slim: bool = True
) -> CanonicalTraceEvent:
    kind, title, summary = _agent_kind_and_title(entry)
    event = str(entry.get("event") or "other")
    tool = None
    if kind in {"tool_call", "tool_result", "tool_error"}:
        tool = _tool_from_entry(entry, error=_opt_text(entry.get("error")))
        if tool and (tool.input is not None or tool.output is not None or tool.error):
            summary = _truncate(tool.error or tool.output or tool.input) or summary

    raw, truncated = _raw_payload(entry, slim=slim)
    return CanonicalTraceEvent(
        id=f"agent-{index}",
        timestamp=_timestamp(entry.get("timestamp")),
        source="agent",
        kind=kind,
        title=title,
        summary=summary,
        phase=entry.get("phase") if isinstance(entry.get("phase"), str) else None,
        event=event,
        tool=tool,
        raw=raw,
        truncated=truncated,
    )


def adapt_nika_event(
    entry: dict[str, Any], *, index: int, slim: bool = True
) -> CanonicalTraceEvent:
    event = str(entry.get("event") or "system")
    data = entry.get("data")
    if event in {"eval_metrics_saved", "eval_publish"}:
        kind: EventKind = "score"
    elif event in _LIFECYCLE_EVENTS:
        kind = "lifecycle"
    elif event == "system":
        kind = "system"
    else:
        kind = "lifecycle" if event else "other"

    duration_ms = entry.get("duration_ms")
    if duration_ms is None and isinstance(data, dict):
        duration_ms = data.get("duration_ms")
    try:
        duration_val = float(duration_ms) if duration_ms is not None else None
    except (TypeError, ValueError):
        duration_val = None

    raw, truncated = _raw_payload(entry, slim=slim)
    return CanonicalTraceEvent(
        id=f"nika-{index}",
        timestamp=_timestamp(entry.get("timestamp")),
        source="nika",
        kind=kind,
        # Same text as the CLI line (nika.utils.logger.format_event_line);
        # ``data`` stays in ``raw`` for the detail view.
        title=event,
        summary=event_summary(entry),
        event=event,
        duration_ms=duration_val,
        raw=raw,
        truncated=truncated,
    )


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    # Live writers may leave a partial UTF-8 sequence at the end of the file.
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _annotate_claude_request_durations(
    entries: list[dict[str, Any]], events: list[CanonicalTraceEvent]
) -> None:
    """Annotate Claude API responses with request and per-block timing.

    Claude stream-json has no request bookends. Content blocks of one response
    share ``message.id``; the request is sent right after the preceding entry
    (tool results or ``system init``) and completes with its last block. Each
    block is logged once fully generated, so its generation starts where the
    previous block of the same response ended (``start_timestamp``). The
    request elapsed goes on the response's first llm row (``duration_ms``).
    """
    groups: dict[str, list[int]] = {}
    for i, entry in enumerate(entries):
        claude = _claude_event(entry) or {}
        message = claude.get("message")
        if claude.get("type") == "assistant" and isinstance(message, dict):
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                groups.setdefault(message_id, []).append(i)
    for indices in groups.values():
        if indices[0] == 0:
            continue
        request_start = _timestamp(entries[indices[0] - 1].get("timestamp"))
        block_start = request_start
        for i in indices:
            events[i].start_timestamp = block_start
            block_start = events[i].timestamp
        target = next((i for i in indices if events[i].kind == "llm"), None)
        if target is None or request_start is None:
            continue
        try:
            start = datetime.fromisoformat(request_start)
            end = datetime.fromisoformat(entries[indices[-1]]["timestamp"])
            elapsed_ms = (end - start).total_seconds() * 1000
        except (KeyError, TypeError, ValueError):
            continue
        if elapsed_ms > 0:
            events[target].duration_ms = elapsed_ms


def _attach_phase_prompts(
    entries: list[dict[str, Any]], events: list[CanonicalTraceEvent]
) -> None:
    """Show a phase's ``prompt`` row as the input of its first LLM turn.

    Agents log the phase's initial model input (system prompt + task) once as
    a ``prompt`` row, which has no LLM turn of its own. Kept unslimmed so the
    full prompt is readable. Only the phase's first model output qualifies;
    if that is a tool call, no turn gets the prompt.
    """
    pending: tuple[Any, str] | None = None
    for entry, event in zip(entries, events):
        if entry.get("event") == "prompt" and isinstance(entry.get("text"), str):
            pending = (entry.get("phase"), entry["text"])
        elif pending is not None and event.kind in {"llm", "tool_call"}:
            phase, text = pending
            if event.kind == "llm" and entry.get("phase") == phase:
                event.raw["prompt"] = text
            pending = None


def load_agent_events(
    session_dir: Path, *, slim: bool = True
) -> list[CanonicalTraceEvent]:
    path = session_dir / "messages.jsonl"
    entries = list(iter_jsonl(path))
    events = [
        adapt_agent_event(entry, index=i, slim=slim) for i, entry in enumerate(entries)
    ]
    _annotate_claude_request_durations(entries, events)
    _attach_phase_prompts(entries, events)
    return events


def _is_llm_start(ev: CanonicalTraceEvent) -> bool:
    return ev.event in {"llm_start", "turn.started"}


def _is_llm_end(ev: CanonicalTraceEvent) -> bool:
    return ev.event in {"llm_end", "llm_end_error", "turn.completed", "turn.failed"}


def _llm_run_id(ev: CanonicalTraceEvent) -> str | None:
    run_id = ev.raw.get("run_id")
    return run_id if isinstance(run_id, str) and run_id else None


def _event_ms(ev: CanonicalTraceEvent) -> float | None:
    if not ev.timestamp:
        return None
    try:
        return datetime.fromisoformat(ev.timestamp).timestamp() * 1000
    except ValueError:
        return None


def _pair_llm_ends(events: list[CanonicalTraceEvent]) -> dict[str, int]:
    """``start.id -> index of its end``; mirrors ``pairLlmEnds`` in roles.ts.

    Logged LangChain ``run_id``s pair concurrent calls exactly; without them
    the end must come before the next start, and must share the phase.
    """
    pairs: dict[str, int] = {}
    used: set[str] = set()
    for i, start in enumerate(events):
        if not _is_llm_start(start):
            continue
        run_id = _llm_run_id(start)
        for j in range(i + 1, len(events)):
            ev = events[j]
            if ev.id in used or ev.event == "llm_retry":
                continue
            if run_id:
                if _is_llm_end(ev) and _llm_run_id(ev) == run_id:
                    pairs[start.id] = j
                    used.add(ev.id)
                    break
                continue
            if not _is_llm_end(ev):
                if _is_llm_start(ev):
                    break
                continue
            if _llm_run_id(ev):
                continue
            if start.phase and ev.phase and ev.phase != start.phase:
                continue
            pairs[start.id] = j
            used.add(ev.id)
            break
    return pairs


def llm_request_durations(events: list[CanonicalTraceEvent]) -> list[float]:
    """Completed LLM request wall times (ms) from agent events, in log order.

    Same rows the Timeline ledger counts (``llmDurationStats`` in roles.ts):
    ``llm_start``/Codex ``turn.started`` paired with their end, split at HTTP
    ``llm_retry`` markers into one duration per attempt; Claude responses,
    whose request elapsed sits on the first block (``duration_ms``); and a
    start that never ended but was superseded by a later start (closed at
    that start). Unfinished trailing requests contribute nothing.
    """
    pairs = _pair_llm_ends(events)
    durations: list[float] = []
    starts = [(i, ev) for i, ev in enumerate(events) if _is_llm_start(ev)]
    next_start_ms: dict[str, float | None] = {}
    for k, (_, ev) in enumerate(starts):
        later = starts[k + 1][1] if k + 1 < len(starts) else None
        next_start_ms[ev.id] = _event_ms(later) if later is not None else None
    for i, ev in enumerate(events):
        if ev.source != "agent":
            continue
        if ev.kind == "llm" and not _is_llm_start(ev) and not _is_llm_end(ev):
            if ev.duration_ms is not None and ev.duration_ms > 0:
                durations.append(float(ev.duration_ms))
            continue
        if not _is_llm_start(ev):
            continue
        end_index = pairs.get(ev.id)
        run_id = _llm_run_id(ev)
        until = end_index if end_index is not None else len(events)
        points: list[float | None] = [_event_ms(ev)]
        for j in range(i + 1, until):
            retry = events[j]
            if retry.event != "llm_retry":
                continue
            retry_run = _llm_run_id(retry)
            if run_id and retry_run and retry_run != run_id:
                continue
            points.append(_event_ms(retry))
        if end_index is not None:
            points.append(_event_ms(events[end_index]))
        else:
            # Superseded by the next request: closed at that start.
            points.append(next_start_ms.get(ev.id))
        for a, b in zip(points, points[1:]):
            if a is not None and b is not None and b >= a:
                durations.append(b - a)
    return durations


def _span_ms(start: Any, end: Any) -> float | None:
    try:
        first = datetime.fromisoformat(str(_timestamp(start)))
        last = datetime.fromisoformat(str(_timestamp(end)))
        return round((last - first).total_seconds() * 1000, 1)
    except (TypeError, ValueError):
        return None


def _fold_progress(entries: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """Collapse each run of the same ``*_progress`` event into one row.

    The row keeps the last step (the final CLI line of that run), spans
    first→last via ``duration_ms``, and lists every step under ``steps``.
    """
    out: list[tuple[int, dict[str, Any]]] = []
    index = 0
    for event, group in groupby(entries, key=lambda entry: entry.get("event")):
        rows = list(group)
        if len(rows) > 1 and str(event).endswith("_progress"):
            folded = dict(rows[-1])
            span = _span_ms(rows[0].get("timestamp"), rows[-1].get("timestamp"))
            if span is not None:
                folded["duration_ms"] = span
            folded["steps"] = [
                {"timestamp": row.get("timestamp"), "message": row.get("message")}
                for row in rows
            ]
            out.append((index + len(rows) - 1, folded))
        else:
            out.extend(enumerate(rows, start=index))
        index += len(rows)
    return out


def load_nika_events(
    session_dir: Path, *, slim: bool = True
) -> list[CanonicalTraceEvent]:
    entries = list(iter_jsonl(session_dir / "nika.jsonl"))
    return [
        adapt_nika_event(entry, index=i, slim=slim)
        for i, entry in _fold_progress(entries)
    ]
