"""Canonical token counts for ``messages.jsonl`` and ``eval_metrics``.

``input_tokens`` is uncached prompt tokens plus Anthropic cache creation/read.
OpenAI-style ``prompt_tokens`` already include cached tokens; those are not
added again. LangChain ``input_token_details`` is a breakdown of
``input_tokens``, not an extra count.
"""

from __future__ import annotations

from typing import Any


def _get_int(usage: Any, key: str) -> int:
    if usage is None:
        return 0
    value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(usage: Any | None) -> dict[str, int]:
    """Return ``{input_tokens, output_tokens}`` from provider usage payloads."""
    input_tokens = _get_int(usage, "input_tokens") or _get_int(usage, "prompt_tokens")
    output_tokens = _get_int(usage, "output_tokens") or _get_int(
        usage, "completion_tokens"
    )
    input_tokens += _get_int(usage, "cache_creation_input_tokens")
    input_tokens += _get_int(usage, "cache_read_input_tokens")
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}
