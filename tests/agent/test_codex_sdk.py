from __future__ import annotations
import pytest
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

    def test_local_auth_detection(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr(
            "agent.sdk.codex_sdk.config.sbx_openai_credential_available",
            lambda: False,
            raising=False,
        )
        # Patch the credentials import path used inside the function.
        monkeypatch.setattr(
            "agent.sandbox.sbx.credentials.sbx_openai_credential_available",
            lambda: False,
            raising=False,
        )
        auth_file = tmp_path / ".codex" / "auth.json"
        auth_file.parent.mkdir(parents=True)
        auth_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(
            "agent.sdk.codex_sdk.config.Path.home", lambda: tmp_path
        )
        assert codex_sdk_local_auth_available()
