"""Canonical token counts for ``messages.jsonl`` and ``eval_metrics``.

``input_tokens`` is uncached prompt tokens plus Anthropic cache creation/read.
OpenAI-style ``prompt_tokens`` already include cached tokens; those are not
added again. LangChain ``input_token_details`` is a breakdown of
``input_tokens``, not an extra count.

``reasoning_tokens`` is a provider-reported breakdown of billed output
(OpenAI / Anthropic / LangChain / Codex ``reasoning_output_tokens``: a subset
of ``output_tokens``, never added to it). Never estimate from thinking text.

Codex ``ThreadTokenUsage`` carries ``last`` (one model response) and ``total``
(thread cumulative). Callers that log per response pass ``last`` (or a
``total`` delta) explicitly; :func:`normalize_usage` reads ``total`` when given
the wrapper so a single thread-level record is never undercounted.
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


def _detail_int(usage: Any, *path: str) -> int:
    """Read a nested int from dicts or attribute objects (provider usage details)."""
    cur: Any = usage
    for key in path:
        if cur is None:
            return 0
        cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
    try:
        return int(cur or 0)
    except (TypeError, ValueError):
        return 0


def _unwrap_usage(usage: Any | None) -> Any | None:
    """Read Codex ``ThreadTokenUsage.total``, else the flat usage record."""
    if usage is None:
        return None
    total = (
        usage.get("total") if isinstance(usage, dict) else getattr(usage, "total", None)
    )
    if total is not None:
        return total
    return usage


def _reasoning_tokens(usage: Any | None) -> int:
    """Provider-reported reasoning/thinking tokens only (never inferred from text)."""
    return (
        _get_int(usage, "reasoning_tokens")
        or _get_int(usage, "reasoning_output_tokens")
        or _detail_int(usage, "output_token_details", "reasoning")
        or _detail_int(usage, "output_tokens_details", "reasoning_tokens")
        or _detail_int(usage, "output_tokens_details", "thinking_tokens")
        or _detail_int(usage, "completion_tokens_details", "reasoning_tokens")
    )


def normalize_usage(usage: Any | None) -> dict[str, int]:
    """Return ``{input_tokens, output_tokens, reasoning_tokens}`` from provider usage.

    ``reasoning_tokens`` is a breakdown of output when the provider supplies it.
    OpenAI Responses (and therefore Codex), Chat Completions, and Anthropic
    already include reasoning in ``output_tokens``; it is never added again.
    """
    usage = _unwrap_usage(usage)
    input_tokens = _get_int(usage, "input_tokens") or _get_int(usage, "prompt_tokens")
    output_tokens = _get_int(usage, "output_tokens") or _get_int(
        usage, "completion_tokens"
    )
    input_tokens += _get_int(usage, "cache_creation_input_tokens")
    input_tokens += _get_int(usage, "cache_read_input_tokens")
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": _reasoning_tokens(usage),
    }
