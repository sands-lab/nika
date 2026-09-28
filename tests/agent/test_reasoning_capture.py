"""Tests for reasoning / thinking capture into messages.jsonl."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from agent.protocols import DIAGNOSIS
from agent.utils.loggers import AgentCallbackLogger
from agent.utils.reasoning_capture import (
    attach_openai_reasoning,
    extract_reasoning_text,
    reasoning_fields_for_log,
)


def test_extract_reasoning_from_openai_compat_fields() -> None:
    msg = SimpleNamespace(
        content="answer",
        reasoning="think via attr",
        model_extra=None,
        additional_kwargs={},
        content_blocks=None,
    )
    assert extract_reasoning_text(msg) == "think via attr"

    nested = SimpleNamespace(
        content="answer",
        model_extra={"reasoning": "from model_extra"},
        additional_kwargs={},
        content_blocks=None,
    )
    assert extract_reasoning_text(nested) == "from model_extra"


def test_extract_reasoning_from_anthropic_content_blocks() -> None:
    msg = AIMessage(
        content=[
            {"type": "thinking", "thinking": "step 1"},
            {"type": "text", "text": "done"},
        ]
    )
    assert extract_reasoning_text(msg) == "step 1"
    assert reasoning_fields_for_log(msg) == {"reasoning_content": "step 1"}


def test_extract_reasoning_from_additional_kwargs() -> None:
    msg = AIMessage(
        content="answer",
        additional_kwargs={"reasoning_content": "hidden think"},
    )
    assert extract_reasoning_text(msg) == "hidden think"


def test_attach_openai_reasoning_copies_model_extra() -> None:
    ai = AIMessage(content="4")
    openai_msg = SimpleNamespace(
        content="4",
        model_extra={"reasoning": "2+2"},
        additional_kwargs=None,
        content_blocks=None,
    )
    attach_openai_reasoning(ai, openai_msg)
    assert ai.additional_kwargs["reasoning_content"] == "2+2"


def test_agent_callback_logger_writes_reasoning_content(tmp_path: Path) -> None:
    logger = AgentCallbackLogger(phase=DIAGNOSIS, session_dir=str(tmp_path))
    message = AIMessage(
        content="final answer",
        additional_kwargs={"reasoning_content": "chain of thought"},
        usage_metadata={
            "input_tokens": 3,
            "output_tokens": 5,
            "total_tokens": 8,
        },
    )
    generation = ChatGeneration(
        message=message, generation_info={"finish_reason": "stop"}
    )
    response = LLMResult(generations=[[generation]])

    logger.on_llm_end(response)

    entry = json.loads(
        (tmp_path / "messages.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()[-1]
    )
    assert entry["event"] == "llm_end"
    assert entry["text"] == "final answer"
    assert entry["reasoning_content"] == "chain of thought"
    assert entry["usage_metadata"] == {
        "input_tokens": 3,
        "output_tokens": 5,
        "reasoning_tokens": 0,
    }
