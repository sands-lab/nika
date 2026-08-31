from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nika.utils.session_store import SessionStore
from nika.workflows.benchmark.resume import cleanup_benchmark_session
from nika.workflows.session.close import close_session


def _write_run_json(session_dir: Path, *, session_id: str, lab_name: str) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "run.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "scenario_name": "simple_bgp",
                "lab_name": lab_name,
                "session_dir": str(session_dir),
                "status": "running",
                "backend": "kathara",
                "scenario_params": {"lab_name": lab_name, "backend": "kathara"},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_close_undeploys_when_runtime_json_is_gone(tmp_path: Path, monkeypatch) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr("nika.utils.session_store.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("nika.utils.session_store.SESSIONS_DB", db_path)

    session_id = "simple_bgp__link_down__deadbeef__t01"
    session_dir = tmp_path / "results" / "trials" / session_id
    lab_name = "simple_bgp__leftover"
    _write_run_json(session_dir, session_id=session_id, lab_name=lab_name)

    store = SessionStore(sessions_dir, db_path)
    store.create_session(
        {
            "session_id": session_id,
            "scenario_name": "simple_bgp",
            "lab_name": lab_name,
            "session_dir": str(session_dir),
            "status": "running",
            "backend": "kathara",
            "scenario_params": {"lab_name": lab_name, "backend": "kathara"},
        }
    )
    store.delete_session(session_id)
    assert not (sessions_dir / f"{session_id}.json").exists()

    env = MagicMock()
    env.lab_exists.return_value = True
    env.backend = "kathara"
    with (
        patch("nika.remote.config.is_remote_enabled", return_value=False),
        patch("nika.workflows.session.close.get_net_env_instance", return_value=env),
    ):
        close_session(session_id=session_id, session_dir=session_dir)

    env.undeploy.assert_called()


@pytest.mark.unit
def test_wipe_force_clears_retired_scenario_and_continues(
    tmp_path: Path, monkeypatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    db_path = tmp_path / "sessions.db"
    store = SessionStore(sessions_dir, db_path)
    monkeypatch.setattr(
        "nika.workflows.session.close.SessionStore", lambda *args, **kwargs: store
    )
    monkeypatch.setattr(
        "nika.utils.session.SessionStore", lambda *args, **kwargs: store
    )

    session_dirs: dict[str, Path] = {}
    for session_id, scenario in (
        ("current-session", "dc_clos"),
        ("retired-session", "retired_scenario"),
    ):
        session_dir = tmp_path / "results" / session_id
        session_dirs[session_id] = session_dir
        session_dir.mkdir(parents=True)
        meta = {
            "session_id": session_id,
            "scenario_name": scenario,
            "lab_name": f"{scenario}__test",
            "session_dir": str(session_dir),
            "status": "running",
            "backend": "kathara",
            "scenario_params": {"backend": "kathara"},
        }
        (session_dir / "run.json").write_text(json.dumps(meta), encoding="utf-8")
        store.create_session(meta)

    assert store.list_running_sessions()[0]["session_id"] == "retired-session"

    env = MagicMock()
    env.lab_exists.return_value = False

    def load_env(scenario: str, **kwargs):
        if scenario == "retired_scenario":
            raise ValueError("Network environment 'retired_scenario' not found")
        return env

    with (
        patch("nika.remote.config.is_remote_enabled", return_value=False),
        patch(
            "nika.workflows.session.close.get_net_env_instance",
            side_effect=load_env,
        ),
        patch("nika.workflows.session.close.wipe_kathara_labs"),
        patch("nika.workflows.session.close.wipe_all_containerlab_labs"),
        patch("nika.workflows.session.close.wipe_runtime_artifacts", return_value=0),
    ):
        close_session(stop_all=True)

    assert store.list_running_sessions() == []
    for session_dir in session_dirs.values():
        run_meta = json.loads((session_dir / "run.json").read_text(encoding="utf-8"))
        assert run_meta["status"] == "finished"


@pytest.mark.unit
def test_cleanup_undeploys_before_deleting_run_json(
    tmp_path: Path, monkeypatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr("nika.utils.session_store.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("nika.utils.session_store.SESSIONS_DB", db_path)
    monkeypatch.setattr("nika.workflows.benchmark.resume.SESSIONS_DIR", sessions_dir)

    session_id = "simple_bgp__link_flap__cafebabe__t01"
    session_dir = tmp_path / "results" / "trials" / session_id
    lab_name = "simple_bgp__stale"
    _write_run_json(session_dir, session_id=session_id, lab_name=lab_name)

    env = MagicMock()
    env.lab_exists.return_value = True
    env.backend = "kathara"
    with (
        patch("nika.remote.config.is_remote_enabled", return_value=False),
        patch("nika.workflows.session.close.get_net_env_instance", return_value=env),
    ):
        cleanup_benchmark_session(session_id, session_dir)

    env.undeploy.assert_called()
    assert not session_dir.exists()


@pytest.mark.unit
def test_close_does_not_delete_a_sibling_session_json(
    tmp_path: Path, monkeypatch
) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr("nika.utils.session_store.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("nika.utils.session_store.SESSIONS_DB", db_path)

    store = SessionStore(sessions_dir, db_path)
    monkeypatch.setattr(
        "nika.utils.session.SessionStore",
        lambda *args, **kwargs: SessionStore(sessions_dir, db_path),
    )
    monkeypatch.setattr(
        "nika.workflows.session.close.SessionStore",
        lambda *args, **kwargs: SessionStore(sessions_dir, db_path),
    )
    keep_id = "simple_bgp__link_flap__aaaa1111__t01"
    close_id = "simple_bgp__link_down__bbbb2222__t01"
    for sid, lab_name in (
        (keep_id, "simple_bgp__keep"),
        (close_id, "simple_bgp__close"),
    ):
        session_dir = tmp_path / "results" / "trials" / sid
        _write_run_json(session_dir, session_id=sid, lab_name=lab_name)
        store.create_session(
            {
                "session_id": sid,
                "scenario_name": "simple_bgp",
                "lab_name": lab_name,
                "session_dir": str(session_dir),
                "status": "running",
                "backend": "kathara",
                "scenario_params": {"lab_name": lab_name, "backend": "kathara"},
            }
        )

    env = MagicMock()
    env.lab_exists.return_value = True
    env.backend = "kathara"
    close_dir = tmp_path / "results" / "trials" / close_id
    with (
        patch("nika.remote.config.is_remote_enabled", return_value=False),
        patch("nika.workflows.session.close.get_net_env_instance", return_value=env),
    ):
        close_session(session_id=close_id, session_dir=close_dir)

    assert (sessions_dir / f"{keep_id}.json").exists()
    assert not (sessions_dir / f"{close_id}.json").exists()


@pytest.mark.unit
def test_update_session_keeps_document_on_lookup_path(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions", tmp_path / "sessions.db")
    store.create_session(
        {
            "session_id": "alpha",
            "scenario_name": "simple_bgp",
            "lab_name": "lab-a",
            "status": "running",
        }
    )
    store.create_session(
        {
            "session_id": "beta",
            "scenario_name": "simple_bgp",
            "lab_name": "lab-b",
            "status": "running",
        }
    )
    store.update_session("alpha", {"session_id": "beta", "lab_name": "lab-a-updated"})

    assert store.get_session("alpha")["lab_name"] == "lab-a-updated"
    assert store.get_session("alpha")["session_id"] == "alpha"
    assert store.get_session("beta")["lab_name"] == "lab-b"


@pytest.mark.unit
def test_mcp_session_meta_falls_back_to_run_json(tmp_path: Path, monkeypatch) -> None:
    from nika.mcp.session_context import get_session_meta

    session_id = "simple_bgp__link_down__feedface__t01"
    results_root = tmp_path / "results"
    session_dir = results_root / session_id
    lab_name = "simple_bgp__live"
    _write_run_json(session_dir, session_id=session_id, lab_name=lab_name)
    monkeypatch.setattr(
        "nika.workflows.session.close.resolve_results_root",
        lambda _path=None: results_root,
    )
    monkeypatch.setenv("NIKA_SESSION_ID", session_id)

    meta = get_session_meta()
    assert meta["lab_name"] == lab_name
    assert meta["session_dir"] == str(session_dir)
