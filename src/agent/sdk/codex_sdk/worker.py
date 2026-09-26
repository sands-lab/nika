"""OpenAI Codex SDK worker — one phase per AsyncCodex thread."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from agent.sandbox.sbx.auth import apply_codex_auth
from agent.cli.codex.codex_display import format_codex_event
from agent.cli.codex.codex_worker import (
    _build_mcp_toml,
    _codex_model_provider_id,
    _codex_provider_base_url,
    prepare_codex_subprocess_env,
)
from agent.sdk.codex_sdk.config import validate_reasoning_effort
from agent.utils.loggers import (
    MessageLogger,
    PendingToolCallTracker,
    tool_event_payload,
)
from agent.utils.mcp_client import load_session_mcp_config
from agent.protocols import PHASES
from agent.utils.skills import prepare_codex_workspace
from agent.utils.two_phase import max_steps_report
from agent.utils.usage import normalize_usage


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
        max_steps: int = 20,
        *,
        llm_provider: str,
        system_prompt: str,
        stream_output: bool = True,
    ) -> None:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")

        self.session_id = session_id
        self.session_dir = session_dir
        self.phase = phase
        self.model = model
        self.llm_provider = llm_provider
        self.reasoning_effort = validate_reasoning_effort(reasoning_effort)
        self.scenario_name = scenario_name
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self._stream_output = stream_output
        self.workspace = Path(session_dir) / "codex_sdk_workspace"
        self._codex_home = self.workspace / ".codex_home"
        self._logger = MessageLogger(phase=phase, session_dir=session_dir)
        self._pending_tool_calls = PendingToolCallTracker()

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

        servers = load_session_mcp_config(
            self.session_id, self.scenario_name, phase=self.phase
        )

        self._logger.log(
            "mcp_config",
            {"phase": self.phase, "servers": list(servers.keys())},
        )
        config_path = self._codex_home / "config.toml"
        config_path.write_text(
            _build_mcp_toml(servers, provider=self.llm_provider),
            encoding="utf-8",
        )

    def _log_codex_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type", "codex_event")
        self._logger.log(event_type, {"codex_event": event})
        if self._stream_output:
            display = format_codex_event(event)
            if display:
                print(display, flush=True)

    async def _collect_turn_with_logging(self, turn: Any, stream: Any) -> str:
        """Log the turn and return its final text under the shared step contract.

        Codex sends ``thread/tokenUsage/updated`` after every model response,
        so each change of ``total`` is one LLM turn: it becomes one ``llm_end``
        carrying the ``total`` delta. After ``max_steps`` responses, the next
        item that starts belongs to a response past the budget, and the turn is
        interrupted.
        """
        from openai_codex._run import (
            _final_assistant_response_from_items,
            _raise_for_failed_turn,
        )
        from openai_codex.generated.v2_all import (
            AgentMessageThreadItem,
            ItemCompletedNotification,
            ItemStartedNotification,
            McpToolCallThreadItem,
            ReasoningThreadItem,
            ThreadTokenUsageUpdatedNotification,
            TurnCompletedNotification,
        )

        turn_id = turn.id
        completed = None
        items = []
        responses = 0
        interrupted = False
        last_total: dict[str, int] = {}
        pending_text: list[str] = []
        pending_reasoning: list[str] = []
        last_text = ""
        # ``run`` logged the first response's llm_start with the prompt.
        awaiting_start = False

        async for event in stream:
            payload = event.payload
            if (
                isinstance(payload, ItemStartedNotification)
                and payload.turn_id == turn_id
            ):
                if responses >= self.max_steps and not interrupted:
                    interrupted = True
                    await turn.interrupt()
                    continue
                if awaiting_start:
                    awaiting_start = False
                    self._logger.log("llm_start", {"model": {"name": self.model}})
                item = _unwrap_thread_item(payload.item)
                if isinstance(item, McpToolCallThreadItem):
                    item_id = getattr(item, "id", None)
                    self._logger.log(
                        "tool_start",
                        self._pending_tool_calls.register(
                            name=item.tool,
                            input=item.arguments,
                            tool_call_id=item_id,
                        ),
                    )
            elif (
                isinstance(payload, ItemCompletedNotification)
                and payload.turn_id == turn_id
            ):
                item = _unwrap_thread_item(payload.item)
                items.append(payload.item)
                if isinstance(item, McpToolCallThreadItem):
                    self._log_mcp_tool_completed(item)
                elif isinstance(item, AgentMessageThreadItem) and item.text:
                    pending_text.append(item.text)
                    last_text = item.text
                    self._log_codex_event(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": item.text},
                        }
                    )
                elif isinstance(item, ReasoningThreadItem):
                    pending_reasoning.extend(item.summary or [])
            elif (
                isinstance(payload, ThreadTokenUsageUpdatedNotification)
                and payload.turn_id == turn_id
            ):
                total = normalize_usage(payload.token_usage.total)
                if total == last_total:
                    continue
                delta = {k: v - last_total.get(k, 0) for k, v in total.items()}
                last_total = total
                responses += 1
                llm_end: dict[str, Any] = {
                    "text": "\n".join(pending_text),
                    "usage_metadata": delta,
                }
                if pending_reasoning:
                    llm_end["reasoning_content"] = "\n\n".join(pending_reasoning)
                self._logger.log("llm_end", llm_end)
                pending_text = []
                pending_reasoning = []
                awaiting_start = True
            elif (
                isinstance(payload, TurnCompletedNotification)
                and payload.turn.id == turn_id
            ):
                completed = payload

        if completed is None:
            raise RuntimeError("turn completed event not received")
        if interrupted:
            return max_steps_report(
                self._logger, max_steps=self.max_steps, latest_text=last_text
            )
        _raise_for_failed_turn(completed.turn)
        return _final_assistant_response_from_items(items) or last_text

    def _log_mcp_tool_completed(self, item: Any) -> None:
        output = _mcp_result_text(item.result)
        item_id = getattr(item, "id", None)
        resolved = self._pending_tool_calls.resolve(
            name=item.tool,
            tool_call_id=item_id,
            input=item.arguments,
        )
        correlation = tool_event_payload(
            name=item.tool or resolved.get("name") or None,
            input=resolved.get("input") or item.arguments,
            tool_call_id=item_id,
        )
        if item.error is not None:
            self._logger.log("tool_error", {**correlation, "output": str(item.error)})
        else:
            self._logger.log(
                "tool_end",
                {**correlation, "output": output, "output_type": "tool_result"},
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
        provider_id = _codex_model_provider_id(self.llm_provider)
        if provider_id and _codex_provider_base_url(self.llm_provider):
            thread_config["model_provider"] = provider_id
        if self.reasoning_effort is not None:
            thread_config["model_reasoning_effort"] = self.reasoning_effort

        sdk_env = prepare_codex_subprocess_env(
            codex_home=self._codex_home,
            provider=self.llm_provider,
            agent_type="sdk.codex_sdk",
        )
        codex_config = CodexConfig(
            env=sdk_env,
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
                    return await self._collect_turn_with_logging(turn, stream)
                finally:
                    await stream.aclose()
        except Exception as exc:
            if self._stream_output:
                print(f"ERROR: {exc}", file=sys.stderr, flush=True)
            return f"ERROR: {exc}"
