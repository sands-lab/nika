"""The task an agent runs, as written by the host into ``task.json``."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent.runtime.contract import ENV_WORKSPACE, TASK_FILENAME


@dataclass(frozen=True)
class LlmClientSettings:
    """Timeout, retries, streaming and sampling of the agent's model client."""

    timeout_sec: float = 480.0
    max_retries: int = 2
    stream: bool = True
    # Sampling. None = not sent, so the server applies the model's defaults.
    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None

    def sampling(self, *, seed: bool = True) -> dict[str, float | int]:
        """The sampling parameters that are set; *seed* ``False`` drops the seed."""
        params = {"temperature": self.temperature, "top_p": self.top_p}
        if seed:
            params["seed"] = self.seed
        return {key: value for key, value in params.items() if value is not None}


@dataclass(frozen=True)
class Task:
    """Everything an agent needs to run one phase.

    ``mcp_servers`` maps each MCP server of the session to its HTTP client
    entry (``transport``, ``url``, ``headers``); the gateway, not this list,
    decides which servers a phase may call.
    """

    session_id: str
    agent_class: str
    task_description: str
    mcp_servers: dict[str, dict[str, Any]]
    model: str | None = None
    llm_provider: str | None = None
    max_steps: int = 20
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    stream_output: bool = True
    scenario_name: str = ""
    llm: LlmClientSettings = field(default_factory=LlmClientSettings)
    # 0 disables the MCP read timeout.
    mcp_read_timeout_sec: float = 120.0
    enable_skills: bool = False

    def to_json(self) -> str:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["llm"] = {f.name: getattr(self.llm, f.name) for f in fields(self.llm)}
        return json.dumps(data, indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        known = {f.name for f in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["llm"] = LlmClientSettings(**(data.get("llm") or {}))
        return cls(**values)


def workspace_dir() -> Path:
    """The agent workspace: ``$AGENT_WORKSPACE``, else the working directory."""
    return Path(os.environ.get(ENV_WORKSPACE, "").strip() or os.getcwd()).resolve()


@lru_cache(maxsize=1)
def _load(path: Path) -> Task:
    return Task.from_dict(json.loads(path.read_text(encoding="utf-8")))


def current_task() -> Task:
    """The task of this sandbox, read once from ``task.json``."""
    path = workspace_dir() / TASK_FILENAME
    if not path.is_file():
        raise RuntimeError(f"No {TASK_FILENAME} in the agent workspace {path.parent}")
    return _load(path)
