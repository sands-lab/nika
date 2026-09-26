"""HTTP-level LLM retry markers for inspect timeline splitting."""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import HumanMessage

from agent.utils.loggers import AgentCallbackLogger, log_llm_retry


def test_log_llm_retry_writes_marker(tmp_path: Path) -> None:
    cb = AgentCallbackLogger(phase="diagnosis", session_dir=str(tmp_path))
    cb.on_chat_model_start(
        {"id": ["ChatOpenAI"]},
        [[HumanMessage(content="hi")]],
        run_id="run-1",
    )
    log_llm_retry(
        TimeoutError("timed out"),
        failed_attempt=1,
        max_retries=2,
    )
    lines = (tmp_path / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    start = json.loads(lines[0])
    retry = json.loads(lines[1])
    assert start["event"] == "llm_start"
    assert retry["event"] == "llm_retry"
    assert retry["run_id"] == "run-1"
    assert retry["failed_attempt"] == 1
    assert retry["next_attempt"] == 2
    cb.on_llm_error(RuntimeError("give up"), run_id="run-1")


def test_pop_active_llm_across_contexts(tmp_path: Path) -> None:
    """LangGraph may finish the LLM call in a different ContextVar context."""
    import contextvars

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    cb = AgentCallbackLogger(phase="diagnosis", session_dir=str(tmp_path))
    start_ctx = contextvars.copy_context()
    start_ctx.run(
        cb.on_chat_model_start,
        {"id": ["ChatOpenAI"]},
        [[HumanMessage(content="hi")]],
        run_id="run-x",
    )
    response = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="ok"))]]
    )
    # Must not raise ValueError("Token ... created in a different Context").
    cb.on_llm_end(response, run_id="run-x")
    events = [
        json.loads(line)
        for line in (tmp_path / "messages.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["event"] == "llm_end"
