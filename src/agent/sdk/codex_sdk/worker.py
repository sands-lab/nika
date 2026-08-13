"""OpenAI Codex SDK worker — one phase per AsyncCodex thread."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent.sandbox.sbx.auth import apply_codex_auth
from agent.cli.codex.codex_display import format_codex_event
from agent.cli.codex.codex_worker import _build_mcp_toml
from agent.sdk.codex_sdk.config import validate_reasoning_effort
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config
from agent.protocols import PHASES, SUBMISSION
from agent.utils.skills import prepare_codex_workspace


def _unwrap_thread_item(item: Any) -> Any:
    return item.root if hasattr(item, "root") else item


def _mcp_result_text(result: Any) -> str:
    if result is None:
        return ""
    if hasattr(result, "model_dump"):
        data = result.model_dump()
    elif isinstance(result, dict):
        data = result
    else:
        return str(result)
    content = data.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(data)


class CodexSdkWorker:
    """Drive one troubleshooting phase via ``openai-codex``."""

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        phase: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        scenario_name: str = "",
        *,
        system_prompt: str,
        stream_output: bool = True,
    ) -> None:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")

        self.session_id = session_id
        self.session_dir = session_dir
        self.phase = phase
        self.model = model
        self.reasoning_effort = validate_reasoning_effort(reasoning_effort)
        self.scenario_name = scenario_name
        self.system_prompt = system_prompt
        self._stream_output = stream_output
        self.workspace = Path(session_dir) / "codex_sdk_workspace"
        self._codex_home = self.workspace / ".codex_home"
        self._logger = MessageLogger(agent=phase, session_dir=session_dir)

    def _setup_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._codex_home.mkdir(parents=True, exist_ok=True)

        if not (self.workspace / ".git").exists():
            subprocess.run(
                ["git", "init", "-q"],
                cwd=self.workspace,
                check=True,
                capture_output=True,
            )

        apply_codex_auth(self._codex_home)

        prepare_codex_workspace(self.workspace)

        if self.phase == SUBMISSION:
            begin_submission_mcp_phase(self.session_id)
        servers = load_session_mcp_config(
            self.session_id,
            self.scenario_name,
        )

        self._logger.log(
            "mcp_config",
            {"phase": self.phase, "servers": list(servers.keys())},
        )
        config_path = self._codex_home / "config.toml"
        config_path.write_text(_build_mcp_toml(servers), encoding="utf-8")

    def _log_codex_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "codex_event")
        self._logger.log(event_type, {"codex_event": event})
        if self._stream_output:
            display = format_codex_event(event)
            if display:
                print(display, flush=True)

    async def _collect_turn_with_logging(self, stream: Any, *, turn_id: str) -> Any:
        from openai_codex._run import (
            TurnResult,
            _final_assistant_response_from_items,
            _raise_for_failed_turn,
        )
        from openai_codex.generated.v2_all import (
            AgentMessageThreadItem,
            ItemCompletedNotification,
            ItemStartedNotification,
            McpToolCallThreadItem,
            ThreadTokenUsageUpdatedNotification,
            TurnCompletedNotification,
        )

        completed = None
        items = []
        usage = None
        agent_text: list[str] = []

        async for event in stream:
            payload = event.payload
            if (
                isinstance(payload, ItemStartedNotification)
                and payload.turn_id == turn_id
            ):
                item = _unwrap_thread_item(payload.item)
                if isinstance(item, McpToolCallThreadItem):
                    self._logger.log(
                        "tool_start",
                        {
                            "tool": {"name": item.tool},
                            "input": json.dumps(item.arguments, ensure_ascii=False)
                            if item.arguments is not None
                            else "{}",
                        },
                    )
                    self._log_codex_event(
                        {
                            "type": "item.started",
                            "item": {
                                "type": "mcp_tool_call",
                                "server": item.server,
                                "tool": item.tool,
                                "arguments": item.arguments,
                            },
                        }
                    )
            elif (
                isinstance(payload, ItemCompletedNotification)
                and payload.turn_id == turn_id
            ):
                item = _unwrap_thread_item(payload.item)
                items.append(payload.item)
                if isinstance(item, McpToolCallThreadItem):
                    output = _mcp_result_text(item.result)
                    if item.error is not None:
                        self._logger.log("tool_error", {"output": str(item.error)})
                    else:
                        self._logger.log(
                            "tool_end",
                            {"output": output, "output_type": "tool_result"},
                        )
                    self._log_codex_event(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "mcp_tool_call",
                                "server": item.server,
                                "tool": item.tool,
                                "status": getattr(
                                    item.status, "value", str(item.status)
                                ),
                                "result": output,
                            },
                        }
                    )
                elif isinstance(item, AgentMessageThreadItem) and item.text:
                    agent_text.append(item.text)
                    self._log_codex_event(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": item.text},
                        }
                    )
            elif (
                isinstance(payload, ThreadTokenUsageUpdatedNotification)
                and payload.turn_id == turn_id
            ):
                usage = payload.token_usage
            elif (
                isinstance(payload, TurnCompletedNotification)
                and payload.turn.id == turn_id
            ):
                completed = payload

        if completed is None:
            raise RuntimeError("turn completed event not received")

        _raise_for_failed_turn(completed.turn)
        turn = completed.turn
        final_response = _final_assistant_response_from_items(items) or "\n".join(
            agent_text
        )
        if final_response:
            usage_md: dict[str, int] = {}
            if usage is not None:
                usage_md = {
                    "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                }
            self._logger.log(
                "llm_end", {"text": final_response, "usage_metadata": usage_md}
            )

        return TurnResult(
            id=turn.id,
            status=turn.status,
            error=turn.error,
            started_at=turn.started_at,
            completed_at=turn.completed_at,
            duration_ms=turn.duration_ms,
            final_response=final_response,
            items=items,
            usage=usage,
        )

    async def run(self, prompt: str) -> str:
        try:
            from openai_codex import AsyncCodex, CodexConfig, Sandbox
        except ImportError as exc:
            raise RuntimeError(
                "openai-codex is not installed. Run: uv sync --extra sdk --prerelease=allow"
            ) from exc

        self._setup_workspace()

        self._logger.log(
            "llm_start",
            {
                "messages": {"role": "user", "content": prompt[:500]},
                "model": {"name": self.model},
            },
        )

        thread_config: dict[str, str] = {}
        if self.reasoning_effort is not None:
            thread_config["model_reasoning_effort"] = self.reasoning_effort

        codex_config = CodexConfig(
            env={"CODEX_HOME": str(self._codex_home)},
            cwd=str(self.workspace),
        )

        try:
            async with AsyncCodex(config=codex_config) as codex:
                thread = await codex.thread_start(
                    model=self.model,
                    cwd=str(self.workspace),
                    developer_instructions=self.system_prompt,
                    config=thread_config or None,
                    sandbox=Sandbox.workspace_write,
                )
                turn = await thread.turn(prompt)
                stream = turn.stream()
                try:
                    result = await self._collect_turn_with_logging(
                        stream, turn_id=turn.id
                    )
                finally:
                    await stream.aclose()
        except Exception as exc:
            self._logger.log("agent_error", {"phase": self.phase, "error": str(exc)})
            if self._stream_output:
                print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            return f"ERROR: {exc}"

        final = result.final_response or ""
        self._logger.log(
            "agent_done", {"phase": self.phase, "report_length": len(final)}
        )
        return final
