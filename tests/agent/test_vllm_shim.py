import pytest

from agent.cli.claude.vllm_shim import fold_system_turns

pytestmark = pytest.mark.unit


def test_body_without_system_turns_is_unchanged() -> None:
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert fold_system_turns(body) is body


def test_system_turn_after_user_is_appended_to_that_user_turn() -> None:
    body = {
        "system": "top-level prompt",
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "system", "content": "reminder"},
        ],
    }
    folded = fold_system_turns(body)
    assert folded["system"] == "top-level prompt"
    assert folded["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "q"},
                {
                    "type": "text",
                    "text": "<system-reminder>\nreminder\n</system-reminder>",
                },
            ],
        }
    ]


def test_system_turn_after_assistant_moves_to_next_user_turn() -> None:
    tool_result = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            {"role": "system", "content": [{"type": "text", "text": "note"}]},
            {"role": "user", "content": [tool_result]},
        ]
    }
    messages = fold_system_turns(body)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"] == [
        tool_result,
        {"type": "text", "text": "<system-reminder>\nnote\n</system-reminder>"},
    ]


def test_trailing_system_turn_becomes_user_turn() -> None:
    body = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            {"role": "system", "content": "late"},
        ]
    }
    messages = fold_system_turns(body)["messages"]
    assert messages[-1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "<system-reminder>\nlate\n</system-reminder>"}
        ],
    }
