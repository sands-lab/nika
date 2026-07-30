from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from agent.registry import create_agent


def test_non_byo_agent_cannot_run_on_host() -> None:
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="only run inside the Docker sandbox"):
            create_agent(
                "cli.codex",
                session_id="test-session",
                model="test-model",
            )


def test_unknown_agent_still_reports_unsupported_type() -> None:
    with pytest.raises(ValueError, match="Unsupported agent type"):
        create_agent("unknown", session_id="test-session", model="test-model")
