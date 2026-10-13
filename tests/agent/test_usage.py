from __future__ import annotations

import pytest

from types import SimpleNamespace

from agent.utils.usage import normalize_usage

pytestmark = pytest.mark.unit


def _u(inp: int, out: int, reason: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "reasoning_tokens": reason,
    }


class UsageNormalizeTest:
    def test_anthropic_cache_fields_fold_into_input(self) -> None:
        assert normalize_usage(
            {
                "input_tokens": 16262,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 107648,
                "output_tokens": 11272,
            }
        ) == _u(123910, 11272)

    def test_anthropic_thinking_tokens_are_subset_not_added(self) -> None:
        assert normalize_usage(
            {
                "input_tokens": 100,
                "output_tokens": 500,
                "output_tokens_details": {"thinking_tokens": 400},
            }
        ) == _u(100, 500, 400)

    def test_openai_prompt_tokens_already_include_cache(self) -> None:
        assert normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
            }
        ) == _u(100, 20)

    def test_openai_reasoning_tokens_are_subset_not_added(self) -> None:
        assert normalize_usage(
            {
                "prompt_tokens": 50,
                "completion_tokens": 200,
                "completion_tokens_details": {"reasoning_tokens": 150},
            }
        ) == _u(50, 200, 150)

    def test_langchain_input_token_details_are_not_added_again(self) -> None:
        assert normalize_usage(
            {
                "input_tokens": 123910,
                "output_tokens": 11272,
                "input_token_details": {
                    "cache_read": 107648,
                    "cache_creation": 0,
                },
            }
        ) == _u(123910, 11272)

    def test_langchain_output_token_details_reasoning(self) -> None:
        assert normalize_usage(
            {
                "input_tokens": 10,
                "output_tokens": 100,
                "output_token_details": {"reasoning": 70},
            }
        ) == _u(10, 100, 70)

    def test_object_attributes(self) -> None:
        usage = SimpleNamespace(input_tokens=10, output_tokens=4)
        assert normalize_usage(usage) == _u(10, 4)

    def test_none_and_empty(self) -> None:
        assert normalize_usage(None) == _u(0, 0)
        assert normalize_usage({}) == _u(0, 0)

    def test_codex_reasoning_is_subset_of_output(self) -> None:
        # OpenAI Responses (and Codex) output_tokens already include reasoning.
        usage = SimpleNamespace(
            input_tokens=1000,
            output_tokens=50,
            reasoning_output_tokens=10,
            cached_input_tokens=100,
            total_tokens=1050,
        )
        assert normalize_usage(usage) == _u(1000, 50, 10)

    def test_codex_thread_token_usage_reads_total(self) -> None:
        assert normalize_usage(
            {
                "last": {"input_tokens": 100, "output_tokens": 20},
                "total": {
                    "input_tokens": 999,
                    "output_tokens": 99,
                    "reasoning_output_tokens": 9,
                },
            }
        ) == _u(999, 99, 9)

    def test_codex_thread_token_usage_falls_back_to_total(self) -> None:
        assert normalize_usage(
            {"total": {"input_tokens": 40, "output_tokens": 8}}
        ) == _u(40, 8)

    def test_does_not_infer_reasoning_from_text_fields(self) -> None:
        assert normalize_usage(
            {
                "input_tokens": 1,
                "output_tokens": 2,
                "reasoning_content": "a" * 1000,
                "thinking": "b" * 1000,
            }
        ) == _u(1, 2)
