"""Unit tests for parent-side sbx cleanup after hard-killed workers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.sandbox.sbx.cleanup import cleanup_sbx_for_session
from agent.sandbox.sbx.policy import sanitize_sandbox_name
from agent.sandbox.sbx.workspace import opaque_agent_workspace_dir

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_cleanup_sbx_for_session_targets_only_own_sandbox(
    tmp_path: Path, monkeypatch
) -> None:
    agent_sid = "20260926-120000-a-abcdef"
    monkeypatch.setattr(
        "agent.sandbox.sbx.workspace.RUNTIME_DIR",
        tmp_path / "runtime",
    )
    workspace = opaque_agent_workspace_dir(agent_sid)
    workspace.mkdir(parents=True)
    (workspace / "messages.jsonl").write_text("x\n", encoding="utf-8")
    # Sibling workspace from another concurrent trial must stay.
    other_sid = "20260926-120000-a-ffffff"
    other = opaque_agent_workspace_dir(other_sid)
    other.mkdir(parents=True)
    (other / "keep").write_text("1", encoding="utf-8")

    rm_calls: list[list[str]] = []

    def _fake_rm(args):
        rm_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="Sandbox removed\n", stderr="")

    with (
        patch("agent.sandbox.sbx.client.sbx_available", return_value=True),
        patch("agent.sandbox.sbx.client.run_sbx_optional", side_effect=_fake_rm),
    ):
        result = cleanup_sbx_for_session(
            {
                "session_id": "simple_bgp__link_down__t01",
                "agent_session_id": agent_sid,
            }
        )

    assert result is not None
    assert result.sandbox_name == sanitize_sandbox_name(agent_sid)
    assert result.removed_sandbox is True
    assert result.removed_workspace is True
    assert rm_calls == [["rm", "--force", sanitize_sandbox_name(agent_sid)]]
    assert not workspace.exists()
    assert other.is_dir()


@pytest.mark.unit
def test_cleanup_sbx_for_session_skips_when_already_gone(
    tmp_path: Path, monkeypatch
) -> None:
    agent_sid = "20260926-120000-a-dead01"
    monkeypatch.setattr(
        "agent.sandbox.sbx.workspace.RUNTIME_DIR",
        tmp_path / "runtime",
    )

    with (
        patch("agent.sandbox.sbx.client.sbx_available", return_value=True),
        patch(
            "agent.sandbox.sbx.client.run_sbx_optional",
            return_value=SimpleNamespace(
                returncode=0, stdout="", stderr="sandbox not found"
            ),
        ) as rm,
    ):
        result = cleanup_sbx_for_session({"agent_session_id": agent_sid})

    assert result is not None
    assert result.did_work is False
    rm.assert_called_once_with(["rm", "--force", sanitize_sandbox_name(agent_sid)])
