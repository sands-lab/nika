from __future__ import annotations

import pytest

from agent.sdk.codex_sdk.config import validate_reasoning_effort

pytestmark = pytest.mark.unit


class CodexSdkConfigTest:
    """Reasoning-effort validation for sdk.codex_sdk."""

    def test_validate_reasoning_effort_accepts_valid(self) -> None:
        assert validate_reasoning_effort("medium") == "medium"

    def test_validate_reasoning_effort_rejects_invalid(self) -> None:
        with pytest.raises(ValueError):
            validate_reasoning_effort("invalid")
