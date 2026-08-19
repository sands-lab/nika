from __future__ import annotations
import pytest
import unittest.mock
from agent.sdk.codex_sdk.config import (
    codex_sdk_local_auth_available,
    validate_reasoning_effort,
)
from tests.support.integration_pipeline import load_test_env

load_test_env()


class CodexSdkConfigTest:
    """Local auth and reasoning-effort validation for sdk.codex_sdk."""

    def test_validate_reasoning_effort_accepts_valid(self) -> None:
        assert validate_reasoning_effort("medium") == "medium"

    def test_validate_reasoning_effort_rejects_invalid(self) -> None:
        with pytest.raises(ValueError):
            validate_reasoning_effort("invalid")

    def test_local_auth_detection(self) -> None:
        with unittest.mock.patch("agent.sdk.codex_sdk.config.Path") as mock_path:
            mock_home = mock_path.home.return_value
            mock_home.__truediv__.return_value.is_file.return_value = True
            assert codex_sdk_local_auth_available()
