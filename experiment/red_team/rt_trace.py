"""Trace helpers for the Red Team: tool calls, report JSON, citations, replay.

Copied from ``experiment/healthy_hunter`` (``hh_trace.py``, ``review.py``,
``hunt.py``) on branch feat/healthy-hunter-baseline-audit so this branch runs
on its own; merge the two copies into shared experiment code once both land.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Per-call output cap in the tool-free final prompt after the turn budget.
FINALIZE_OUTPUT_CHARS = 1500


# Commands that change lab state. A trace containing any of these taints the
# environment (the Hunter must observe only).
_MUTATING = re.compile(
    r"""
    \bip\s+(-\d\s+)?(link|addr|address|route|rule|neigh|neighbor)\s+(set|add|del|delete|change|replace|flush)\b
    | \b(ifconfig|ifup|ifdown)\b\s+\S+\s+(up|down|\d)
    | \b(systemctl|service)\s+\S*\s*(start|stop|restart|reload|kill|disable|enable)\b
    | \b(kill|pkill|killall|reboot|shutdown|halt)\b
    | \b(iptables|ip6tables|nft|ebtables)\b[^|;&]*\s(?-i:-A|-D|-I|-R|-F|-X|-N|-P|add|delete|flush|insert|replace)\b
    | \btc\s+(qdisc|class|filter)\s+(add|del|change|replace)\b
    | \bvtysh\b.*(conf(igure)?\s+t(erminal)?|\bclear\b|write\s+mem)
    | \b(sysctl\s+-w|ethtool\s+-[sKA]|brctl\s+(addif|delif|addbr|delbr)|ovs-(vsctl|ofctl)\s+(add|del|mod|set))\b
    | \bkubectl\s+(apply|delete|edit|patch|scale|create|replace|label|annotate|cordon|drain|rollout)\b
    | (^|[;&|]\s*)(rm|mv|cp|tee|sed\s+-i|truncate|dd)\s
    | >\s*/(etc|proc|sys)/
    | \bcommit\b
    """,
    re.VERBOSE | re.IGNORECASE,
)


def short_tool_name(name: str) -> str:
    """``mcp__server__tool`` -> ``tool``; other names pass through."""
    if name.startswith("mcp__"):
        return name.split("__", 2)[-1]
    return name


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return json.dumps(content)


def load_tool_calls(messages_path: Path) -> list[dict[str, Any]]:
    """Return ordered tool calls ``{id, tool, name, args, output, is_error}``."""
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    if not messages_path.is_file():
        return []
    for line in messages_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = (record.get("data") or record).get("claude_event") or record.get(
            "claude_event"
        )
        if not isinstance(event, dict):
            continue
        content = (event.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[block["id"]] = {
                    "id": block["id"],
                    "name": block.get("name", ""),
                    "tool": short_tool_name(block.get("name", "")),
                    "args": block.get("input") or {},
                    "output": None,
                    "is_error": None,
                }
                order.append(block["id"])
            elif block.get("type") == "tool_result":
                call = calls.get(block.get("tool_use_id", ""))
                if call is not None:
                    call["output"] = _result_text(block.get("content"))
                    call["is_error"] = bool(block.get("is_error"))
    return [calls[i] for i in order]


def mutating_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hits = []
    for call in calls:
        text = " ".join(str(v) for v in call["args"].values())
        if call["tool"] in {"packet_capture_start"}:
            continue
        if _MUTATING.search(text):
            hits.append({"id": call["id"], "tool": call["tool"], "args": call["args"]})
    return hits


def extract_report_json(report: str) -> dict[str, Any] | None:
    """Parse the last fenced ```json block of the Hunter's final answer."""
    blocks = re.findall(r"```json\s*(.*?)```", report or "", re.DOTALL)
    for block in reversed(blocks):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            # The model sometimes writes JavaScript-style \' escapes.
            try:
                data = json.loads(block.replace("\\'", "'"))
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            return data
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _args_equal(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def check_evidence(item: dict, calls: list[dict]) -> dict[str, Any]:
    """Classify one evidence item against the trace."""
    tool = str(item.get("tool") or "").split("__")[-1]
    args = item.get("args") or {}
    excerpt = _norm(item.get("excerpt"))
    same_tool = [c for c in calls if c["tool"] == tool]
    exact = [c for c in same_tool if _args_equal(c["args"], args)]
    excerpt_hit = lambda c: bool(excerpt) and excerpt in _norm(c["output"])  # noqa: E731
    if exact and any(excerpt_hit(c) for c in exact):
        status = "verified"
    elif any(excerpt_hit(c) for c in same_tool):
        status = "excerpt_in_other_call"
    elif exact:
        status = "excerpt_not_found"
    elif any(excerpt_hit(c) for c in calls):
        status = "excerpt_in_other_tool"
    else:
        status = "fabricated"
    matched = next((c for c in exact if excerpt_hit(c)), exact[0] if exact else None)
    return {
        "tool": tool,
        "args": args,
        "status": status,
        "matched_call_id": matched["id"] if matched else None,
    }


def citation_status(checks: list[dict]) -> str:
    if not checks:
        return "missing"
    statuses = {c["status"] for c in checks}
    if statuses == {"verified"}:
        return "verified"
    if statuses == {"fabricated"}:
        return "fabricated"
    return "partial"


async def call_tools(servers: dict, wanted: list[dict]) -> list[dict]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    tool_server: dict[str, str] = {}
    out = []
    for name, cfg in servers.items():
        async with streamablehttp_client(cfg["url"], headers=cfg["headers"]) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for tool in (await session.list_tools()).tools:
                    tool_server[tool.name] = name
    for item in wanted:
        server = tool_server.get(item["tool"])
        if server is None:
            out.append({**item, "output": None, "error": "tool not mounted"})
            continue
        cfg = servers[server]
        try:
            async with streamablehttp_client(cfg["url"], headers=cfg["headers"]) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool(item["tool"], item["args"])
                    text = "\n".join(
                        getattr(block, "text", "") for block in res.content or []
                    )
                    out.append({**item, "output": text, "is_error": res.isError})
        except Exception as exc:  # noqa: BLE001 - replay failure is recorded
            out.append(
                {**item, "output": None, "error": f"{type(exc).__name__}: {exc}"}
            )
    return out


def max_steps_reached(messages: Path) -> bool:
    return messages.is_file() and '"event": "max_steps_reached"' in messages.read_text()


def finalize_prompt(prompt: str, calls: list[dict]) -> str:
    """Prompt for a final, tool-free answer after the turn budget ran out."""
    lines = [
        prompt,
        "",
        "## Turn budget used up",
        "",
        "You used your whole turn budget on the investigation below. Do not call"
        " any tools. Using only these tool calls and outputs, write your final"
        " answer now in the required format. Cite evidence only from this list,"
        " with the exact arguments and verbatim excerpts.",
        "",
    ]
    for idx, call in enumerate(calls, start=1):
        output = (call["output"] or "")[:FINALIZE_OUTPUT_CHARS]
        lines += [f"### Call {idx}: {call['tool']} {json.dumps(call['args'])}"]
        lines += ["```", output, "```", ""]
    return "\n".join(lines)


def finalize_report(prompt: str, config: Path) -> str:
    """One tool-free completion from the configured endpoint and model.

    Claude Code always mounts the MCP tools, and the model kept calling them
    when asked for a final answer, so this request offers no tools at all.
    """
    import httpx
    import yaml

    agent = yaml.safe_load(config.read_text())["agent"]
    response = httpx.post(
        f"{agent['custom']['base_url'].rstrip('/')}/chat/completions",
        json={
            "model": agent["model"],
            "max_tokens": agent["max_tokens"],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=600,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"] or ""


def mem_available_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0
