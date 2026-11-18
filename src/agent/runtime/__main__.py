"""Run one phase of the task's agent: ``python -m agent.runtime <phase>``.

``diagnosis`` writes the report to ``report.json``. ``submission`` reads it
and ``submission_context.json``, both written by the host in between.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import sys
from typing import Any

from agent.protocols import DIAGNOSIS, PHASES
from agent.runtime.contract import (
    REPORT_FILENAME,
    RUNTIME_ENV_FILENAME,
    SUBMISSION_CONTEXT_FILENAME,
)
from agent.runtime.task import Task, current_task, workspace_dir


def _apply_runtime_env() -> None:
    path = workspace_dir() / RUNTIME_ENV_FILENAME
    if not path.is_file():
        return
    for key, value in json.loads(path.read_text(encoding="utf-8")).items():
        if value is not None:
            os.environ[key] = str(value)
    # The interpreter already started: import roots (an added agent's project)
    # must also reach this process's sys.path.
    for root in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if root and root not in sys.path:
            sys.path.append(root)


def create_agent(task: Task) -> Any:
    """Instantiate ``task.agent_class`` with the settings its constructor takes."""
    module_name, _, attr = task.agent_class.partition(":")
    agent_class = getattr(importlib.import_module(module_name), attr)
    settings = {
        "session_id": task.session_id,
        "model": task.model,
        "llm_provider": task.llm_provider,
        "max_steps": task.max_steps,
        "reasoning_effort": task.reasoning_effort,
        "max_tokens": task.max_tokens,
        "stream_output": task.stream_output,
    }
    params = inspect.signature(agent_class).parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        settings = {key: value for key, value in settings.items() if key in params}
    return agent_class(**settings)


def main(phase: str) -> None:
    if phase not in PHASES:
        raise SystemExit(f"Usage: python -m agent.runtime {{{'|'.join(PHASES)}}}")
    _apply_runtime_env()
    task = current_task()
    agent = create_agent(task)
    workspace = workspace_dir()
    if phase == DIAGNOSIS:
        report = asyncio.run(agent.run_diagnosis(task.task_description))
        (workspace / REPORT_FILENAME).write_text(
            json.dumps({"report": report}, ensure_ascii=False), encoding="utf-8"
        )
        return
    report = json.loads((workspace / REPORT_FILENAME).read_text(encoding="utf-8"))
    context = json.loads(
        (workspace / SUBMISSION_CONTEXT_FILENAME).read_text(encoding="utf-8")
    )
    asyncio.run(agent.run_submission(report["report"], context))


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except Exception as exc:
        print(f"Agent runner failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
