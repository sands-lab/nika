from __future__ import annotations

import pytest

from agent.sdk.claude_sdk.config import resolve_claude_sdk_model

pytestmark = pytest.mark.unit


class ClaudeSdkConfigTest:
    """Model resolution for sdk.claude_sdk."""

    def test_resolve_claude_sdk_model_explicit(self) -> None:
        assert resolve_claude_sdk_model("custom-model") == "custom-model"

    def test_resolve_claude_sdk_model_requires_one(self) -> None:
        with pytest.raises(ValueError):
            resolve_claude_sdk_model("  ")
