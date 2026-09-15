from __future__ import annotations

from types import SimpleNamespace

from agent.utils.usage import normalize_usage


class UsageNormalizeTest:
    def test_anthropic_cache_fields_fold_into_input(self) -> None:
        assert normalize_usage(
            {
                "input_tokens": 16262,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 107648,
                "output_tokens": 11272,
            }
        ) == {"input_tokens": 123910, "output_tokens": 11272}

    def test_openai_prompt_tokens_already_include_cache(self) -> None:
        assert normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
            }
        ) == {"input_tokens": 100, "output_tokens": 20}

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
        ) == {"input_tokens": 123910, "output_tokens": 11272}

    def test_object_attributes(self) -> None:
        usage = SimpleNamespace(input_tokens=10, output_tokens=4)
        assert normalize_usage(usage) == {"input_tokens": 10, "output_tokens": 4}

    def test_none_and_empty(self) -> None:
        assert normalize_usage(None) == {"input_tokens": 0, "output_tokens": 0}
        assert normalize_usage({}) == {"input_tokens": 0, "output_tokens": 0}

    def test_codex_thread_token_usage_unwraps_last(self) -> None:
        usage = SimpleNamespace(
            last=SimpleNamespace(
                input_tokens=1000,
                output_tokens=50,
                reasoning_output_tokens=10,
                cached_input_tokens=100,
                total_tokens=1060,
            ),
            total=SimpleNamespace(
                input_tokens=5000,
                output_tokens=200,
                reasoning_output_tokens=0,
            ),
        )
        assert normalize_usage(usage) == {"input_tokens": 1000, "output_tokens": 60}

    def test_codex_thread_token_usage_dict_prefers_last(self) -> None:
        assert normalize_usage(
            {
                "last": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 5,
                },
                "total": {"input_tokens": 999, "output_tokens": 99},
            }
        ) == {"input_tokens": 100, "output_tokens": 25}

    def test_codex_thread_token_usage_falls_back_to_total(self) -> None:
        assert normalize_usage(
            {"total": {"input_tokens": 40, "output_tokens": 8}}
        ) == {"input_tokens": 40, "output_tokens": 8}
