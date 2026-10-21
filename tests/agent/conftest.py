"""Fixtures for agent code, which reads its task from the sandbox workspace."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.runtime import task as task_module
from agent.runtime.contract import ENV_WORKSPACE, TASK_FILENAME
from agent.runtime.task import Task

SESSION_SERVERS = {
    "kathara_base_mcp_server": {
        "transport": "http",
        "url": "http://gw:9000/mcp/kathara_base_mcp_server/mcp",
        "headers": {"X-Session-Id": "sess", "X-Session-Token": "tok"},
    },
    "task_mcp_server": {
        "transport": "http",
        "url": "http://gw:9000/mcp/task_mcp_server/mcp",
        "headers": {"X-Session-Id": "sess", "X-Session-Token": "tok"},
    },
}


@pytest.fixture
def agent_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Path]]:
    """Write ``task.json`` into a workspace and point the agent at it.

    Call it with :class:`~agent.runtime.task.Task` fields to override; it
    returns the workspace directory.
    """
    workspace = tmp_path / "workspace"

    def write(**fields: Any) -> Path:
        values: dict[str, Any] = {
            "session_id": "sess",
            "agent_class": "agent.base:DiagnosisAgent",
            "task_description": "task",
            "mcp_servers": SESSION_SERVERS,
            "scenario_name": "simple_bgp",
        }
        values.update(fields)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / TASK_FILENAME).write_text(
            Task.from_dict(values).to_json(), encoding="utf-8"
        )
        monkeypatch.setenv(ENV_WORKSPACE, str(workspace))
        task_module._load.cache_clear()
        _clear_timeout_cache()
        return workspace

    yield write
    task_module._load.cache_clear()
    _clear_timeout_cache()


def _clear_timeout_cache() -> None:
    from agent.utils import mcp_client

    mcp_client._mcp_read_timeout.cache_clear()
