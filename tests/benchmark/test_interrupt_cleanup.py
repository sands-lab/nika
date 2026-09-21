"""Ctrl+C during benchmark must undeploy labs and not count as agent_failed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nika.workflows.benchmark.run import (
    cleanup_benchmark_interrupt,
    run_single_case,
)


def test_cleanup_benchmark_interrupt_closes_only_result_dir_sessions(
    tmp_path: Path,
) -> None:
    result_root = tmp_path / "results" / "run_a"
    other_root = tmp_path / "results" / "run_b"
    (result_root / "trials" / "case__t01").mkdir(parents=True)
    (other_root / "trials" / "other__t01").mkdir(parents=True)

    running = [
        {
            "session_id": "case__t01",
            "session_dir": str(result_root / "trials" / "case__t01"),
        },
        {
            "session_id": "other__t01",
            "session_dir": str(other_root / "trials" / "other__t01"),
        },
        {
            "session_id": "orphan__t01",
            "session_dir": None,
        },
    ]
    # orphan under result_root/trials by id only
    (result_root / "trials" / "orphan__t01").mkdir(parents=True)

    close_calls: list[dict] = []

    def _close(*, session_id, undeploy, session_dir):
        close_calls.append(
            {
                "session_id": session_id,
                "undeploy": undeploy,
                "session_dir": Path(session_dir),
            }
        )

    with (
        patch(
            "nika.workflows.benchmark.run.SessionStore"
        ) as store_cls,
        patch(
            "nika.workflows.benchmark.run.close_session",
            side_effect=_close,
        ),
        patch(
            "nika.workflows.benchmark.run._terminate_active_trial_workers"
        ) as terminate,
    ):
        store_cls.return_value.list_running_sessions.return_value = running
        closed = cleanup_benchmark_interrupt(result_root)

    terminate.assert_called_once_with()
    assert closed == 2
    closed_ids = {c["session_id"] for c in close_calls}
    assert closed_ids == {"case__t01", "orphan__t01"}
    assert all(c["undeploy"] is True for c in close_calls)


def test_cleanup_skips_foreign_session_with_same_trial_id(tmp_path: Path) -> None:
    """Deterministic trial ids must not close another run's live session."""
    result_root = tmp_path / "results" / "run_a"
    other_root = tmp_path / "results" / "run_b"
    shared_id = "campus_lan__link_down__s__t01"
    # Leftover incomplete dir under this run (same id as the foreign session).
    (result_root / "trials" / shared_id).mkdir(parents=True)
    (other_root / "trials" / shared_id).mkdir(parents=True)

    running = [
        {
            "session_id": shared_id,
            "session_dir": str(other_root / "trials" / shared_id),
        }
    ]
    with (
        patch("nika.workflows.benchmark.run.SessionStore") as store_cls,
        patch("nika.workflows.benchmark.run.close_session") as close_mock,
        patch("nika.workflows.benchmark.run._terminate_active_trial_workers"),
    ):
        store_cls.return_value.list_running_sessions.return_value = running
        closed = cleanup_benchmark_interrupt(result_root)

    assert closed == 0
    close_mock.assert_not_called()


def test_run_single_case_interrupt_closes_and_reraises(tmp_path: Path) -> None:
    session_dir = tmp_path / "trials" / "dc_clos__x__t01"
    session_dir.mkdir(parents=True)
    (session_dir / "ground_truth.json").write_text("{}", encoding="utf-8")
    session_id = "dc_clos__x__t01"

    with (
        patch(
            "nika.workflows.benchmark.run.start_net_env",
            return_value=session_id,
        ),
        patch("nika.workflows.benchmark.run.inject_failure"),
        patch(
            "nika.workflows.benchmark.run.start_agent",
            side_effect=KeyboardInterrupt(),
        ),
        patch("nika.workflows.benchmark.run.Session") as session_cls,
        patch(
            "nika.workflows.benchmark.run.close_session"
        ) as close_mock,
        patch(
            "nika.workflows.benchmark.run._finalize_agent_failed_trial"
        ) as finalize_mock,
        patch(
            "nika.workflows.benchmark.run.validate_inject_params"
        ),
        patch(
            "nika.workflows.benchmark.run.is_healthy_case",
            return_value=False,
        ),
        patch(
            "nika.workflows.benchmark.run.scenario_requires_topo_size",
            return_value=True,
        ),
    ):
        session_cls.return_value.load_running_session.return_value = MagicMock()
        with pytest.raises(KeyboardInterrupt):
            run_single_case(
                problem="link_down",
                scenario="dc_clos",
                topo_size="m",
                agent_type="byo.langgraph",
                llm_provider="openai",
                model="gpt-5-mini",
                max_steps=5,
                inject_params={"host_name": "client_0"},
                result_dir=str(tmp_path),
                trial_id=session_id,
                trial_index=1,
                case_key="dc_clos__x",
            )

    close_mock.assert_called_once_with(
        session_id=session_id,
        undeploy=True,
        session_dir=session_dir,
    )
    finalize_mock.assert_not_called()


def test_run_trials_batch_interrupt_shuts_down_without_waiting(
    tmp_path: Path,
) -> None:
    from nika.workflows.benchmark.run import Trial, _run_trials_batch

    trial = Trial(
        case_index=0,
        trial_index=1,
        row={"scenario": "dc_clos", "problem": "x", "inject": {}},
        case_key="dc_clos__x",
        trial_id="dc_clos__x__t01",
    )

    with (
        patch(
            "nika.workflows.benchmark.run._run_trial_with_timeout",
            side_effect=KeyboardInterrupt(),
        ),
        patch(
            "nika.workflows.benchmark.run.cleanup_benchmark_interrupt"
        ) as cleanup,
        patch(
            "nika.workflows.benchmark.run.ThreadPoolExecutor"
        ) as pool_cls,
    ):
        pool = MagicMock()
        pool_cls.return_value = pool
        future = MagicMock()
        future.result.side_effect = KeyboardInterrupt()
        pool.submit.return_value = future

        with (
            patch(
                "nika.workflows.benchmark.run.as_completed",
                return_value=[future],
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            _run_trials_batch(
                [trial, trial],
                continue_on_error=False,
                case_timeout=0,
                agent_type="byo.langgraph",
                llm_provider=None,
                model=None,
                max_steps=None,
                result_dir=str(tmp_path),
                session_tag=None,
                release_meta=None,
            )

    cleanup.assert_called()
    pool.shutdown.assert_called_with(wait=False, cancel_futures=True)
