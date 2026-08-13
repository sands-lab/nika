"""File-based session store with SQLite state index.

Each running session is persisted as ``runtime/sessions/{session_id}.json``.
Failure injection records are stored inline under the ``failure_injections``
list field of the same document. A SQLite index at ``runtime/sessions.db``
tracks session state summaries for fast listing via ``nika session ps``.
"""

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nika.config import SESSIONS_DB, SESSIONS_DIR
from nika.utils.session_index import SessionIndex


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionStore:
    def __init__(
        self,
        sessions_dir: str | Path = SESSIONS_DIR,
        db_path: str | Path = SESSIONS_DB,
    ) -> None:
        self.sessions_dir = Path(sessions_dir)
        os.makedirs(self.sessions_dir, exist_ok=True)
        self.index = SessionIndex(db_path)

    def _path(self, session_id: str) -> Path:
        return Path(self.sessions_dir) / f"{session_id}.json"

    def _read(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"Session '{session_id}' not found.")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        path = self._path(data["session_id"])
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Session CRUD
    # ------------------------------------------------------------------

    def create_session(self, values: Mapping[str, Any]) -> None:
        data = dict(values)
        data.setdefault("status", "running")
        now = _now_iso()
        data.setdefault("created_at", now)
        data.setdefault("updated_at", now)
        data.setdefault("failure_injections", [])
        self._write(data)
        self.index.upsert_from_doc(data)

    def update_session(self, session_id: str, values: Mapping[str, Any]) -> None:
        data = self._read(session_id)
        for k, v in values.items():
            if k != "failure_injections":
                data[k] = v
        # Keep the document on the lookup path even if *values* carries another id.
        data["session_id"] = session_id
        data["updated_at"] = _now_iso()
        self._write(data)
        self.index.upsert_from_doc(data)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._read(session_id)

    def delete_session(self, session_id: str) -> None:
        """Remove the runtime session document after results have been persisted."""
        doc: dict[str, Any] | None = None
        path = self._path(session_id)
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            path.unlink()
        if doc is not None:
            doc["status"] = "finished"
            self.index.mark_finished(session_id, doc=doc)
        else:
            self.index.mark_finished(session_id)

    def list_running_sessions(self) -> list[dict[str, Any]]:
        return self.index.list_sessions(running_only=True)

    def list_all_sessions(self) -> list[dict[str, Any]]:
        return self.index.list_sessions(running_only=False)

    def get_unique_running_session(self) -> dict[str, Any]:
        rows = self.list_running_sessions()
        if not rows:
            raise FileNotFoundError(
                "No running session found. Run `nika env run <scenario>` first."
            )
        if len(rows) > 1:
            raise ValueError(
                "Multiple running sessions found. Please pass --session_id to select one."
            )
        return rows[0]

    # ------------------------------------------------------------------
    # Failure injections (stored inline in session document)
    # ------------------------------------------------------------------

    def create_failure_injection(self, values: Mapping[str, Any]) -> int:
        """Append a failure injection record; return its index in the list."""
        session_id = values["session_id"]
        data = self._read(session_id)
        injections: list = data.setdefault("failure_injections", [])
        record = dict(values)
        record.setdefault("status", "pending")
        now = _now_iso()
        record.setdefault("created_at", now)
        record["updated_at"] = now
        idx = len(injections)
        injections.append(record)
        data["updated_at"] = now
        self._write(data)
        self.index.increment_failure_count(session_id)
        return idx

    def update_failure_injection(
        self, session_id: str, failure_idx: int, values: Mapping[str, Any]
    ) -> None:
        """Update the failure injection at ``failure_idx`` within ``session_id``."""
        data = self._read(session_id)
        injections: list = data.get("failure_injections", [])
        if failure_idx >= len(injections):
            raise FileNotFoundError(
                f"Failure injection index {failure_idx} not found in session '{session_id}'."
            )
        injections[failure_idx].update(values)
        injections[failure_idx]["updated_at"] = _now_iso()
        data["updated_at"] = _now_iso()
        self._write(data)

    def list_failure_injections(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if session_id is not None:
            injections = list(self._read(session_id).get("failure_injections", []))
        else:
            injections = []
            for path in Path(self.sessions_dir).glob("*.json"):
                try:
                    injections.extend(
                        json.loads(path.read_text(encoding="utf-8")).get(
                            "failure_injections", []
                        )
                    )
                except Exception:
                    pass
        if status is not None:
            injections = [r for r in injections if r.get("status") == status]
        return sorted(injections, key=lambda r: r.get("created_at", ""), reverse=True)

    def count_failure_statuses(self, *, session_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self._read(session_id).get("failure_injections", []):
            s = str(record.get("status", ""))
            counts[s] = counts.get(s, 0) + 1
        return counts

    def mark_session_failures_ended(
        self, session_id: str, *, end_time: float | None = None
    ) -> int:
        data = self._read(session_id)
        ended_at = end_time if end_time is not None else datetime.now().timestamp()
        now_iso = _now_iso()
        count = 0
        for record in data.get("failure_injections", []):
            if record.get("status") != "ended":
                record["status"] = "ended"
                record["end_time"] = ended_at
                record["updated_at"] = now_iso
                count += 1
        if count > 0:
            data["updated_at"] = now_iso
            self._write(data)
        return count
