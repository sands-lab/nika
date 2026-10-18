"""Extract and normalize LLM reasoning / thinking for ``messages.jsonl``.

Providers surface thinking in different shapes:

* OpenAI-compat (DeepSeek, Qwen/vLLM, OpenRouter): ``reasoning_content`` or
  ``reasoning`` on the chat-completion message (often only in
  ``model_extra``).
* Anthropic / Claude SDK: ``thinking`` content blocks.
* LangChain: ``additional_kwargs["reasoning_content"]`` or list ``content`` /
  ``content_blocks`` with ``thinking`` / ``reasoning`` types.

Inspect reads the canonical top-level ``reasoning_content`` field on
``llm_end`` events (see ``roles.ts``).
"""

from __future__ import annotations

from typing import Any


_REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


def _append_unique(parts: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if not text or text in parts:
        return
    parts.append(text)


def _extend_from_content_blocks(parts: list[str], blocks: Any) -> None:
    if not isinstance(blocks, list):
        return
    for block in blocks:
        if isinstance(block, dict):
            kind = block.get("type")
            if kind in ("thinking", "reasoning", "redacted_thinking"):
                _append_unique(
                    parts,
                    block.get("thinking")
                    or block.get("reasoning")
                    or block.get("text")
                    or block.get("content"),
                )
                continue
            if kind == "non_standard":
                _extend_from_content_blocks(parts, [block.get("value")])
                continue
            continue
        # Claude SDK / pydantic block objects
        block_type = getattr(block, "type", None) or type(block).__name__
        if block_type in ("thinking", "ThinkingBlock", "reasoning", "ReasoningBlock"):
            _append_unique(
                parts,
                getattr(block, "thinking", None)
                or getattr(block, "reasoning", None)
                or getattr(block, "text", None),
            )


def _extend_from_mapping(parts: list[str], data: Any) -> None:
    if not isinstance(data, dict):
        return
    for key in _REASONING_KEYS:
        _append_unique(parts, data.get(key))
    _extend_from_content_blocks(parts, data.get("content"))


def extract_reasoning_text(source: Any) -> str | None:
    """Return joined reasoning/thinking text from a message-like object, if any."""
    if source is None:
        return None

    parts: list[str] = []

    if isinstance(source, dict):
        _extend_from_mapping(parts, source)
        return "\n\n".join(parts) or None

    for key in _REASONING_KEYS:
        _append_unique(parts, getattr(source, key, None))

    _extend_from_mapping(parts, getattr(source, "model_extra", None))
    _extend_from_mapping(parts, getattr(source, "additional_kwargs", None))
    _extend_from_content_blocks(parts, getattr(source, "content", None))
    _extend_from_content_blocks(parts, getattr(source, "content_blocks", None))

    return "\n\n".join(parts) or None


def reasoning_fields_for_log(source: Any) -> dict[str, str]:
    """Payload fragment for ``messages.jsonl`` when reasoning is present."""
    text = extract_reasoning_text(source)
    if not text:
        return {}
    return {"reasoning_content": text}


def attach_openai_reasoning(ai_message: Any, openai_message: Any) -> None:
    """Copy OpenAI-compat reasoning onto a LangChain AIMessage.

    Stores the canonical ``additional_kwargs["reasoning_content"]`` key used by
    LangChain ``content_blocks`` and NIKA inspect.
    """
    if ai_message is None or openai_message is None:
        return
    existing = extract_reasoning_text(ai_message)
    if existing:
        return
    text = extract_reasoning_text(openai_message)
    if not text:
        return
    additional = getattr(ai_message, "additional_kwargs", None)
    if additional is None:
        return
    additional["reasoning_content"] = text
