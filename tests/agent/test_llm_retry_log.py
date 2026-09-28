"""HTTP-level LLM retry markers for inspect timeline splitting."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent.utils.loggers import AgentCallbackLogger, log_llm_retry


class _RetryOnceModel(BaseChatModel):
    """Chat model that reports one HTTP retry from inside the model call."""

    @property
    def _llm_type(self) -> str:
        return "retry-once"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        log_llm_retry(TimeoutError("timed out"), failed_attempt=1, max_retries=2)
        return ChatResult(generations=[ChatGeneration(message=AIMessage("ok"))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize("path", ["sync", "async"])
def test_retry_inside_model_call_is_logged(tmp_path: Path, path: str) -> None:
    cb = AgentCallbackLogger(phase="diagnosis", session_dir=str(tmp_path))
    model = _RetryOnceModel()
    config = {"callbacks": [cb]}
    if path == "sync":
        model.invoke([HumanMessage(content="hi")], config=config)
    else:
        asyncio.run(model.ainvoke([HumanMessage(content="hi")], config=config))
    events = [
        json.loads(line)
        for line in (tmp_path / "messages.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event"] for event in events] == [
        "llm_start",
        "llm_retry",
        "llm_end",
    ]
    retry = events[1]
    assert retry["run_id"] == events[0]["run_id"]
    assert retry["failed_attempt"] == 1
    assert retry["next_attempt"] == 2


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
