"""SQLite index for session state summaries.

Runtime session details remain in JSON files; this module provides fast
listing and status tracking for ``nika session ps`` and related queries.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nika.config import RESULTS_DIR, SESSIONS_DB
from nika.utils.session_artifacts import RUN_FILENAME, iter_session_dirs

GROUND_TRUTH_FILENAME = "ground_truth.json"
EVAL_METRICS_FILENAME = "eval_metrics.json"
LLM_JUDGE_FILENAME = "llm_judge.json"


def _is_finished_session(run_meta: dict) -> bool:
    if run_meta.get("status") == "finished":
        return True
    return run_meta.get("end_time") is not None


_JSON_LIST_FIELDS = frozenset({"problem_names"})

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running',
    lab_name TEXT,
    scenario_name TEXT,
    scenario_topo_size TEXT,
    session_dir TEXT,
    problem_names TEXT,
    root_cause_category TEXT,
    failure_count INTEGER DEFAULT 0,
    agent_type TEXT,
    llm_provider TEXT,
    model TEXT,
    reasoning_effort TEXT,
    start_time TEXT,
    end_time TEXT,
    detection_score REAL,
    localization_f1 REAL,
    rca_f1 REAL,
    in_tokens INTEGER,
    out_tokens INTEGER,
    steps INTEGER,
    tool_calls INTEGER,
    tool_errors INTEGER,
    llm_judge_overall_score REAL,
    created_at TEXT,
    updated_at TEXT
)
"""

_UPSERT_COLUMNS = (
    "session_id",
    "status",
    "lab_name",
    "scenario_name",
    "scenario_topo_size",
    "session_dir",
    "problem_names",
    "root_cause_category",
    "failure_count",
    "agent_type",
    "llm_provider",
    "model",
    "reasoning_effort",
    "start_time",
    "end_time",
    "detection_score",
    "localization_f1",
    "rca_f1",
    "in_tokens",
    "out_tokens",
    "steps",
    "tool_calls",
    "tool_errors",
    "llm_judge_overall_score",
    "created_at",
    "updated_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def extract_index_fields(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Extract index-relevant fields from a session document or run.json."""
    failure_injections = doc.get("failure_injections")
    failure_count = doc.get("failure_count")
    if failure_count is None and failure_injections is not None:
        failure_count = len(failure_injections)

    fields: dict[str, Any] = {
        "session_id": doc.get("session_id"),
        "status": doc.get("status", "running"),
        "lab_name": doc.get("lab_name"),
        "scenario_name": doc.get("scenario_name"),
        "scenario_topo_size": doc.get("scenario_topo_size"),
        "session_dir": doc.get("session_dir"),
        "problem_names": doc.get("problem_names"),
        "root_cause_category": doc.get("root_cause_category"),
        "failure_count": failure_count if failure_count is not None else 0,
        "agent_type": doc.get("agent_type"),
        "llm_provider": doc.get("llm_provider"),
        "model": doc.get("model"),
        "reasoning_effort": doc.get("reasoning_effort"),
        "start_time": doc.get("start_time"),
        "end_time": doc.get("end_time"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
    }

    eval_metrics = doc.get("eval_metrics")
    if isinstance(eval_metrics, dict):
        fields.update(extract_eval_fields(eval_metrics))

    llm_judge = doc.get("llm_judge")
    if isinstance(llm_judge, dict):
        fields.update(extract_eval_fields({}, llm_judge))

    return {
        k: v
        for k, v in fields.items()
        if v is not None or k in ("failure_count", "session_id", "status")
    }


def extract_eval_fields(
    metrics: Mapping[str, Any] | None = None,
    judge: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract eval score fields for the index."""
    fields: dict[str, Any] = {}
    if metrics:
        for key in (
            "detection_score",
            "localization_f1",
            "rca_f1",
            "in_tokens",
            "out_tokens",
            "steps",
            "tool_calls",
            "tool_errors",
        ):
            if key in metrics:
                fields[key] = metrics[key]
    if judge and "overall_score" in judge:
        fields["llm_judge_overall_score"] = judge["overall_score"]
    return fields


def extract_gt_fields(gt: Mapping[str, Any]) -> dict[str, Any]:
    """Extract ground-truth fields for the index from taxonomy metadata."""
    fields: dict[str, Any] = {}
    category = gt.get("root_cause_category") or gt.get("failure_domain")
    if category:
        fields["root_cause_category"] = category
    return fields


class SessionIndex:
    def __init__(self, db_path: str | Path | None = SESSIONS_DB) -> None:
        self.db_path = Path(db_path or SESSIONS_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._maybe_rebuild()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE_SQL)

    def _maybe_rebuild(self) -> None:
        if self.db_path.resolve() != Path(SESSIONS_DB).resolve():
            return
        with self._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        if count == 0:
            self.rebuild_from_results()

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for field in _JSON_LIST_FIELDS:
            if field in data and data[field] is not None:
                data[field] = _json_loads(data[field])
        return data

    def upsert(self, fields: Mapping[str, Any]) -> None:
        session_id = fields.get("session_id")
        if not session_id:
            raise ValueError("session_id is required for index upsert")

        now = _now_iso()
        payload = dict(fields)
        payload.setdefault("updated_at", now)

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if existing is None:
                payload.setdefault("created_at", now)
            elif "created_at" not in payload:
                payload["created_at"] = existing["created_at"]

            columns = [c for c in _UPSERT_COLUMNS if c in payload]
            if not columns:
                return

            placeholders = ", ".join("?" for _ in columns)
            col_names = ", ".join(columns)
            values = []
            for col in columns:
                val = payload[col]
                if col in _JSON_LIST_FIELDS or isinstance(val, list):
                    val = _json_dumps(val)
                values.append(val)

            update_cols = [c for c in columns if c != "session_id"]
            update_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
            conn.execute(
                f"""
                INSERT INTO sessions ({col_names}) VALUES ({placeholders})
                ON CONFLICT(session_id) DO UPDATE SET {update_clause}
                """,
                values,
            )

    def upsert_from_doc(self, doc: Mapping[str, Any]) -> None:
        self.upsert(extract_index_fields(doc))

    def increment_failure_count(self, session_id: str) -> None:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT failure_count, created_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                self.upsert(
                    {"session_id": session_id, "failure_count": 1, "updated_at": now}
                )
                return
            conn.execute(
                """
                UPDATE sessions
                SET failure_count = failure_count + 1, updated_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )

    def mark_finished(
        self, session_id: str, *, doc: Mapping[str, Any] | None = None
    ) -> None:
        fields: dict[str, Any] = {
            "session_id": session_id,
            "status": "finished",
            "updated_at": _now_iso(),
        }
        if doc is not None:
            fields.update(extract_index_fields(doc))
            fields["status"] = "finished"
        self.upsert(fields)

    def purge(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def truncate(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions")

    def get_row(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_sessions(self, *, running_only: bool = True) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if running_only:
                rows = conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE status = 'running'
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM sessions
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def rebuild_from_results(self, results_dir: str | Path | None = None) -> int:
        """Rebuild index rows from ``results/*/run.json`` artifacts."""
        count = 0
        for session_dir in iter_session_dirs(results_dir or RESULTS_DIR):
            run_path = session_dir / RUN_FILENAME
            if not run_path.exists():
                continue
            try:
                run_meta = json.loads(run_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            sid = run_meta.get("session_id") or session_dir.name
            fields = extract_index_fields(run_meta)
            fields["session_id"] = sid
            fields["session_dir"] = str(session_dir)
            if _is_finished_session(run_meta):
                fields["status"] = "finished"
            else:
                fields["status"] = run_meta.get("status", "running")

            gt_path = session_dir / GROUND_TRUTH_FILENAME
            if gt_path.exists():
                try:
                    gt = json.loads(gt_path.read_text(encoding="utf-8"))
                    fields.update(extract_gt_fields(gt))
                except (json.JSONDecodeError, OSError):
                    pass

            metrics_path = session_dir / EVAL_METRICS_FILENAME
            if metrics_path.exists():
                try:
                    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
                    fields.update(extract_eval_fields(metrics))
                except (json.JSONDecodeError, OSError):
                    pass

            judge_path = session_dir / LLM_JUDGE_FILENAME
            if judge_path.exists():
                try:
                    judge = json.loads(judge_path.read_text(encoding="utf-8"))
                    fields.update(extract_eval_fields({}, judge))
                except (json.JSONDecodeError, OSError):
                    pass

            if fields.get("failure_count", 0) == 0:
                problem_names = fields.get("problem_names") or run_meta.get(
                    "problem_names"
                )
                if problem_names:
                    fields["failure_count"] = len(problem_names)

            self.upsert(fields)
            count += 1
        return count
