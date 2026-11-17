"""The session index works on a database a newer NIKA frontend created."""

from __future__ import annotations

import sqlite3

from nika.utils.session_index import SessionIndex
from nika.utils.session_store import SessionStore

# The frontend's table: ``failure_domain`` instead of the ``root_cause_*``
# and ``faulty_devices`` columns.
FRONTEND_TABLE = """
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'running',
    lab_name TEXT, scenario_name TEXT, scenario_topo_size TEXT, session_dir TEXT,
    problem_names TEXT, failure_domain TEXT, failure_count INTEGER DEFAULT 0,
    agent_type TEXT, llm_provider TEXT, model TEXT, reasoning_effort TEXT,
    start_time TEXT, end_time TEXT, detection_score REAL, localization_f1 REAL,
    rca_f1 REAL, in_tokens INTEGER, out_tokens INTEGER, steps INTEGER,
    tool_calls INTEGER, tool_errors INTEGER, llm_judge_overall_score REAL,
    created_at TEXT, updated_at TEXT
)
"""


def test_upsert_skips_columns_the_shared_table_lacks(tmp_path) -> None:
    db = tmp_path / "sessions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(FRONTEND_TABLE)
    index = SessionIndex(db)
    index.upsert(
        {
            "session_id": "s1",
            "status": "running",
            "problem_names": ["link_down"],
            "root_cause_name": "link_down",
            "root_cause_category": "link_failure",
            "faulty_devices": ["pc_0_0"],
        }
    )
    row = index.get_row("s1")
    assert row["problem_names"] == ["link_down"]
    assert "root_cause_name" not in row
    with sqlite3.connect(db) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "root_cause_name" not in columns


def test_new_index_has_the_column_the_frontend_reads(tmp_path) -> None:
    index = SessionIndex(tmp_path / "sessions.db")
    with index._connect() as conn:
        assert "failure_domain" in index._table_columns(conn)


def test_closing_keeps_the_status_the_caller_gives(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions", tmp_path / "sessions.db")
    store.create_session({"session_id": "s1", "session_dir": str(tmp_path)})
    store.delete_session("s1", status="aborted")
    assert store.index.get_row("s1")["status"] == "aborted"
    assert not (tmp_path / "sessions" / "s1.json").exists()
