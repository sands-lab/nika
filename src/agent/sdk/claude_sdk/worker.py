"""Claude Agent SDK worker — one phase per ClaudeSDKClient session."""

from __future__ import annotations

import logging
from typing import Any

from agent.sdk.claude_sdk.config import prepare_claude_sdk_env
from agent.sdk.mcp import to_sdk_mcp_servers
from agent.utils.loggers import MessageLogger, tool_event_payload
from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config
from agent.protocols import PHASES, SUBMISSION
from agent.utils.skills import CLAUDE_SETTING_SOURCES, claude_skills_package_dir
from agent.utils.usage import normalize_usage

logger = logging.getLogger(__name__)


def _normalize_tool_name(name: str) -> str:
    """Map claude-agent-sdk MCP names (``mcp__server__tool``) to short tool ids."""
    prefix = "mcp__"
    if name.startswith(prefix):
        remainder = name[len(prefix) :]
        if "__" in remainder:
            return remainder.split("__", 1)[1]
    return name


class ClaudeSdkWorker:
    """Drive one troubleshooting phase via ``claude-agent-sdk``."""

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        phase: str,
        model: str,
        max_steps: int = 20,
        scenario_name: str = "",
        *,
        llm_provider: str,
        system_prompt: str,
    ) -> None:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")

        self.session_id = session_id
        self.session_dir = session_dir
        self.phase = phase
        self.model = model
        self.llm_provider = llm_provider
        self.max_steps = max_steps
        self.scenario_name = scenario_name
        self.system_prompt = system_prompt
        self._logger = MessageLogger(phase=phase, session_dir=session_dir)

    def _load_mcp_servers(self) -> dict[str, Any]:
        if self.phase == SUBMISSION:
            begin_submission_mcp_phase(self.session_id)
        servers = load_session_mcp_config(
            self.session_id,
            self.scenario_name,
        )
        return to_sdk_mcp_servers(servers)

    async def run(self, prompt: str) -> str:
        try:
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                ResultMessage,
                SystemMessage,
                TextBlock,
                ThinkingBlock,
                ToolResultBlock,
                ToolUseBlock,
                UserMessage,
            )
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk is not installed. Run: uv sync --extra sdk"
            ) from exc

        mcp_servers = self._load_mcp_servers()
        sdk_env = prepare_claude_sdk_env(
            session_id=self.session_id, provider=self.llm_provider
        )

        self._logger.log(
            "mcp_config",
            {"phase": self.phase, "servers": list(mcp_servers.keys())},
        )
        self._logger.log(
            "llm_start",
            {
                "messages": {"role": "user", "content": prompt[:500]},
                "model": {"name": self.model},
                "mcp_servers": list(mcp_servers.keys()),
            },
        )

        options_kwargs: dict[str, Any] = {
            "system_prompt": self.system_prompt,
            "model": self.model,
            "mcp_servers": mcp_servers,
            "max_turns": self.max_steps,
            "permission_mode": "bypassPermissions",
            "env": sdk_env,
        }
        skills_dir = claude_skills_package_dir()
        if skills_dir is not None and self.phase != SUBMISSION:
            options_kwargs["cwd"] = str(skills_dir)
            options_kwargs["setting_sources"] = CLAUDE_SETTING_SOURCES

        options = ClaudeAgentOptions(**options_kwargs)

        result_text = ""
        turn_text: list[str] = []
        tool_names_by_id: dict[str, str] = {}
        tool_inputs_by_id: dict[str, str] = {}

        def _flush_turn() -> None:
            nonlocal turn_text
            if turn_text:
                self._logger.log(
                    "llm_end", {"text": "\n".join(turn_text), "usage_metadata": {}}
                )
                turn_text = []

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(prompt)
                async for message in client.receive_messages():
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        logger.info(
                            "claude_sdk/%s: session started - %s",
                            self.phase,
                            message.data.get("session_id"),
                        )
                    elif isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, (ThinkingBlock, TextBlock)):
                                text = (
                                    block.thinking
                                    if isinstance(block, ThinkingBlock)
                                    else block.text
                                )
                                turn_text.append(text)
                            elif isinstance(block, ToolUseBlock):
                                _flush_turn()
                                tool_name = _normalize_tool_name(block.name)
                                tool_names_by_id[block.id] = tool_name
                                tool_inputs_by_id[block.id] = str(block.input)
                                self._logger.log(
                                    "tool_start",
                                    tool_event_payload(
                                        name=tool_name,
                                        input=block.input,
                                        tool_call_id=block.id,
                                    ),
                                )
                    elif isinstance(message, UserMessage):
                        _flush_turn()
                        content = (
                            message.content if isinstance(message.content, list) else []
                        )
                        for block in content:
                            if isinstance(block, ToolResultBlock):
                                tool_name = tool_names_by_id.get(block.tool_use_id)
                                tool_input = tool_inputs_by_id.get(block.tool_use_id)
                                correlation = tool_event_payload(
                                    name=tool_name,
                                    input=tool_input,
                                    tool_call_id=block.tool_use_id,
                                )
                                if block.is_error:
                                    self._logger.log(
                                        "tool_error",
                                        {
                                            **correlation,
                                            "output": str(block.content),
                                        },
                                    )
                                else:
                                    self._logger.log(
                                        "tool_end",
                                        {
                                            **correlation,
                                            "output": str(block.content),
                                            "output_type": "tool_result",
                                        },
                                    )
                    elif isinstance(message, ResultMessage):
                        _flush_turn()
                        result_text = message.result or ""
                        md = normalize_usage(message.usage)
                        self._logger.log(
                            "llm_end", {"text": result_text, "usage_metadata": md}
                        )
                        logger.info(
                            "claude_sdk/%s: complete - stop_reason=%s",
                            self.phase,
                            message.stop_reason,
                        )
                        break
        except Exception as exc:
            self._logger.log("agent_error", {"phase": self.phase, "error": str(exc)})
            return f"ERROR: {exc}"

        self._logger.log(
            "agent_done", {"phase": self.phase, "report_length": len(result_text)}
        )
        return result_text
