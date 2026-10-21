import pytest


@pytest.fixture(autouse=True)
def _skip_credential_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI tests mock agent runs; provider credentials are not under test."""
    monkeypatch.setattr(
        "nika.agents.sandbox.model_proxy.require_agent_credentials",
        lambda *args, **kwargs: None,
    )
