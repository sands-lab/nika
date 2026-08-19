"""Unit tests for lab cleanup when env start fails or is interrupted."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nika.workflows.env.start import start_net_env


def _run_start_expecting(
    *,
    tmp_path: Path,
    session_id: str,
    verify_side_effect: BaseException,
    lab_exists: bool,
    close_side_effect: BaseException | None = None,
):
    env = MagicMock()
    env.name = "simple_bgp__x"
    env.lab_exists.return_value = lab_exists
    env.metadata = {}
    session = MagicMock()
    session.session_dir = str(tmp_path / "session")

    with ExitStack() as stack:
        stack.enter_context(
            patch("nika.remote.config.is_remote_enabled", return_value=False)
        )
        stack.enter_context(
            patch("nika.workflows.env.start.get_net_env_instance", return_value=env)
        )
        stack.enter_context(
            patch(
                "nika.workflows.env.start.verify_lab_with_retry",
                side_effect=verify_side_effect,
            )
        )
        stack.enter_context(
            patch("nika.workflows.env.start.Session", return_value=session)
        )
        stack.enter_context(patch("nika.workflows.env.start.bind_session_dir"))
        stack.enter_context(patch("nika.workflows.env.start.log_event"))
        stack.enter_context(patch("nika.workflows.env.start.log_error_event"))
        close_mock = stack.enter_context(
            patch(
                "nika.workflows.session.close.close_session",
                side_effect=close_side_effect,
            )
        )
        with pytest.raises(type(verify_side_effect)):
            start_net_env("simple_bgp", None, session_id=session_id)
    return env, close_mock


def test_start_net_env_closes_session_on_keyboard_interrupt(tmp_path: Path) -> None:
    session_id = "20260101-000000-test-abcdef"
    _env, close_mock = _run_start_expecting(
        tmp_path=tmp_path,
        session_id=session_id,
        verify_side_effect=KeyboardInterrupt(),
        lab_exists=False,
    )
    close_mock.assert_called_once_with(session_id=session_id, undeploy=True)


def test_start_net_env_closes_session_on_verify_error(tmp_path: Path) -> None:
    session_id = "20260101-000000-test-fedcba"
    _env, close_mock = _run_start_expecting(
        tmp_path=tmp_path,
        session_id=session_id,
        verify_side_effect=RuntimeError("verify failed"),
        lab_exists=True,
    )
    close_mock.assert_called_once_with(session_id=session_id, undeploy=True)


def test_cleanup_falls_back_to_undeploy_when_close_fails(tmp_path: Path) -> None:
    session_id = "20260101-000000-test-fallback"
    env, close_mock = _run_start_expecting(
        tmp_path=tmp_path,
        session_id=session_id,
        verify_side_effect=RuntimeError("boom"),
        lab_exists=True,
        close_side_effect=RuntimeError("close failed"),
    )
    close_mock.assert_called_once_with(session_id=session_id, undeploy=True)
    env.undeploy.assert_called()
