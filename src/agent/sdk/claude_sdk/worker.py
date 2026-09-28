"""Claude Agent SDK worker — one phase per ClaudeSDKClient session."""

from __future__ import annotations

from typing import Any

from agent.protocols import PHASES, SUBMISSION
from agent.sdk.claude_sdk.config import prepare_claude_sdk_env
from agent.sdk.claude_sdk.transcript import ClaudeSdkTranscript, run_claude_sdk_query
from agent.sdk.mcp import to_sdk_mcp_servers
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.skills import CLAUDE_SETTING_SOURCES, claude_skills_package_dir


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
        max_tokens: int | None = None,
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
        self.max_tokens = max_tokens
        self._logger = MessageLogger(phase=phase, session_dir=session_dir)

    async def run(self, prompt: str) -> str:
        try:
            from claude_agent_sdk import ClaudeAgentOptions
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk is not installed. Run: uv sync --extra sdk"
            ) from exc

        mcp_servers = to_sdk_mcp_servers(
            load_session_mcp_config(
                self.session_id, self.scenario_name, phase=self.phase
            )
        )
        sdk_env = prepare_claude_sdk_env(
            session_id=self.session_id, provider=self.llm_provider
        )
        if self.max_tokens is not None:
            sdk_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_tokens)
        self._logger.log(
            "mcp_config",
            {"phase": self.phase, "servers": list(mcp_servers.keys())},
        )

        options_kwargs: dict[str, Any] = {
            "system_prompt": self.system_prompt,
            "model": self.model,
            "mcp_servers": mcp_servers,
            # claude --max-turns counts model responses: the shared max_steps unit.
            "max_turns": self.max_steps,
            "permission_mode": "bypassPermissions",
            "env": sdk_env,
        }
        skills_dir = claude_skills_package_dir()
        if skills_dir is not None and self.phase != SUBMISSION:
            options_kwargs["cwd"] = str(skills_dir)
            options_kwargs["setting_sources"] = CLAUDE_SETTING_SOURCES

        try:
            return await run_claude_sdk_query(
                ClaudeAgentOptions(**options_kwargs),
                prompt,
                logger=self._logger,
                transcript=ClaudeSdkTranscript(self._logger, model=self.model),
                max_steps=self.max_steps,
            )
        except Exception as exc:
            return f"ERROR: {exc}"
