import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from nika.config import RESULTS_DIR, resolve_results_root
from nika.utils.session_artifacts import (
    RUN_FILENAME,
    is_finished_session,
    iter_session_dirs,
)
from nika.utils.session_resolve import resolve_running_session_id
from nika.utils.session_index import extract_gt_fields, extract_index_fields
from nika.utils.session_store import SessionStore


class Session:
    def __init__(self) -> None:
        self.store = SessionStore()

    def init_session(
        self,
        *,
        session_id: str,
        scenario_name: str,
        lab_name: str,
        scenario_topo_size: str | None,
        scenario_params: dict | None = None,
        result_dir: str | Path | None = None,
        session_dir: str | Path | None = None,
        backend: str = "kathara",
        topology_file: str | Path | None = None,
        runtime_workdir: str | Path | None = None,
    ) -> None:
        self.session_id = session_id
        self.scenario_name = scenario_name
        self.lab_name = lab_name
        self.scenario_topo_size = scenario_topo_size
        self.scenario_params = scenario_params or {}
        self.backend = backend
        self.topology_file = str(topology_file) if topology_file is not None else None
        self.runtime_workdir = (
            str(runtime_workdir) if runtime_workdir is not None else None
        )
        self.scenario_params.setdefault("backend", backend)
        if self.topology_file:
            self.scenario_params.setdefault("topology_file", self.topology_file)
        if self.runtime_workdir:
            self.scenario_params.setdefault("runtime_workdir", self.runtime_workdir)
        if session_dir is not None:
            self.session_dir = str(Path(session_dir))
        else:
            results_root = resolve_results_root(result_dir)
            self.session_dir = os.path.join(str(results_root), session_id)
        os.makedirs(self.session_dir, exist_ok=True)
        self.store.create_session(
            {
                "session_id": self.session_id,
                "lab_name": self.lab_name,
                "scenario_name": self.scenario_name,
                "scenario_topo_size": self.scenario_topo_size,
                "scenario_params": self.scenario_params,
                "session_dir": self.session_dir,
                "status": "running",
                "backend": self.backend,
                "topology_file": self.topology_file,
                "runtime_workdir": self.runtime_workdir,
            }
        )
        self._write_run_json({k: v for k, v in self.__dict__.items() if k != "store"})

    def load_from_run_json(self, session_dir: str | Path) -> "Session":
        """Load session metadata from a mounted ``run.json`` (sandbox execution)."""
        session_path = Path(session_dir)
        run_path = session_path / RUN_FILENAME
        if not run_path.is_file():
            raise FileNotFoundError(f"Missing session run metadata: {run_path}")
        run_meta = json.loads(run_path.read_text(encoding="utf-8"))
        for key, value in run_meta.items():
            setattr(self, key, value)
        self.session_dir = str(session_path.resolve())
        return self

    def load_running_session(self, session_id: str | None = None):
        if os.environ.get("NIKA_SANDBOX_EXECUTION") == "1":
            session_dir = os.environ.get("NIKA_SESSION_DIR", "").strip()
            if session_dir:
                run_path = Path(session_dir) / RUN_FILENAME
                if run_path.is_file():
                    return self.load_from_run_json(session_dir)
        resolved_id = resolve_running_session_id(session_id, store=self.store)
        session_meta = self.store.get_session(resolved_id)
        for key, value in session_meta.items():
            setattr(self, key, value)
        return self

    def load_closed_session(
        self,
        session_id: str | None = None,
        result_dir: str | Path | None = None,
    ):
        """Load a finished session from ``{result_dir}/{session_id}/run.json`` for offline eval."""
        if session_id is not None:
            return self._load_closed_session_from_id(session_id, result_dir=result_dir)

        results_root = resolve_results_root(result_dir)
        candidates: list[tuple[float, dict]] = []
        for session_dir in iter_session_dirs(results_root):
            run_path = session_dir / RUN_FILENAME
            run_meta = json.loads(run_path.read_text(encoding="utf-8"))
            if not is_finished_session(run_meta):
                continue
            sid = run_meta.get("session_id") or session_dir.name
            if self._session_is_still_running(sid):
                continue
            candidates.append((run_path.stat().st_mtime, run_meta))

        if not candidates:
            raise FileNotFoundError(
                f"No closed session found under {results_root}/. "
                "Close a session with `nika session close` first."
            )
        if result_dir is None and len(candidates) > 1:
            raise ValueError(
                "Multiple closed sessions found under results/. Please pass --session_id to select one."
            )
        return self._apply_closed_session_meta(candidates[0][1])

    def _session_is_still_running(self, session_id: str) -> bool:
        try:
            return self.store.get_session(session_id).get("status") == "running"
        except FileNotFoundError:
            return False

    def _load_closed_session_from_id(
        self,
        session_id: str,
        *,
        result_dir: str | Path | None = None,
    ):
        if self._session_is_still_running(session_id):
            raise ValueError(
                f"Session '{session_id}' is still running. Close it with `nika session close` before running eval."
            )

        results_root = resolve_results_root(result_dir)
        session_dir = self._find_closed_session_dir(session_id, result_dir=result_dir)
        run_path = session_dir / RUN_FILENAME
        if not run_path.exists():
            raise FileNotFoundError(
                f"Closed session '{session_id}' not found under {results_root}/. "
                "Close the session with `nika session close` first."
            )

        run_meta = json.loads(run_path.read_text(encoding="utf-8"))
        if not is_finished_session(run_meta):
            raise ValueError(
                f"Session '{session_id}' is not closed. Run `nika session close` before running eval."
            )
        return self._apply_closed_session_meta(run_meta, session_dir=session_dir)

    def _find_closed_session_dir(
        self,
        session_id: str,
        *,
        result_dir: str | Path | None = None,
    ) -> Path:
        results_root = resolve_results_root(result_dir)
        direct = results_root / session_id
        if (direct / RUN_FILENAME).exists():
            return direct
        for session_dir in iter_session_dirs(results_root):
            if session_dir.name == session_id:
                return session_dir
        row = self.store.index.get_row(session_id)
        if row and row.get("session_dir"):
            indexed = Path(row["session_dir"])
            if result_dir is None or indexed.is_relative_to(results_root):
                return indexed
        return direct

    def _apply_closed_session_meta(
        self, run_meta: dict, *, session_dir: Path | None = None
    ):
        for key, value in run_meta.items():
            setattr(self, key, value)
        resolved_dir = session_dir or Path(RESULTS_DIR) / (
            run_meta.get("session_id") or ""
        )
        self.session_dir = str(resolved_dir)
        return self

    def _write_session(self) -> str:
        if not hasattr(self, "session_id"):
            raise ValueError("Session ID is not set.")
        payload = {k: v for k, v in self.__dict__.items() if k != "store"}
        if os.environ.get("NIKA_SANDBOX_EXECUTION") == "1":
            if getattr(self, "session_dir", None):
                self._write_run_json(payload)
            return self.session_id
        try:
            self.store.update_session(self.session_id, payload)
        except FileNotFoundError:
            # Runtime session doc may have been cleared by another process
            # (e.g. concurrent `nika env run` / session close) while a long
            # sandbox agent run was still in flight. Persist results only.
            if getattr(self, "session_dir", None):
                self._write_run_json(payload)
            return self.session_id
        if getattr(self, "session_dir", None):
            self._write_run_json(payload)
        return self.session_id

    def _write_run_json(self, payload: dict) -> None:
        """Write/update run.json in the session results directory."""
        os.makedirs(self.session_dir, exist_ok=True)
        run_path = os.path.join(self.session_dir, "run.json")
        serializable = {
            k: v for k, v in payload.items() if k not in ("store", "failure_injections")
        }
        with open(run_path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2, default=str)

    def update_session(self, key: str, value: Any):
        setattr(self, key, value)
        if hasattr(self, "problem_names") and hasattr(self, "session_id"):
            if len(self.problem_names) > 1:
                self.root_cause_name = "multiple_faults"
            else:
                self.root_cause_name = self.problem_names[0]
        self._write_session()

    def update_run_meta(self, key: str, value: Any):
        """Update ``run.json`` for a closed session (no runtime session document)."""
        setattr(self, key, value)
        if hasattr(self, "problem_names") and hasattr(self, "session_id"):
            if len(self.problem_names) > 1:
                self.root_cause_name = "multiple_faults"
            else:
                self.root_cause_name = self.problem_names[0]
        payload = {k: v for k, v in self.__dict__.items() if k != "store"}
        self._write_run_json(payload)
        if hasattr(self, "session_id"):
            fields = extract_index_fields(payload)
            fields["session_id"] = self.session_id
            fields.setdefault("status", "finished")
            self.store.index.upsert(fields)

    def write_gt(self, gt: dict[str, Any]):
        os.makedirs(self.session_dir, exist_ok=True)
        with open(self.session_dir + "/ground_truth.json", "w") as f:
            f.write(json.dumps(gt, indent=4))
        if hasattr(self, "session_id"):
            self.store.index.upsert(
                {"session_id": self.session_id, **extract_gt_fields(gt)},
            )

    def clear_session(self):
        if not hasattr(self, "session_id"):
            raise ValueError("Session ID is not set.")
        payload = {k: v for k, v in self.__dict__.items() if k != "store"}
        payload["status"] = "finished"
        if getattr(self, "session_dir", None):
            self._write_run_json(payload)
        self.store.delete_session(self.session_id)

    def start_session(self):
        self.start_time = datetime.now().isoformat()
        self._write_session()

    def end_session(self):
        self.end_time = datetime.now().isoformat()
        self._write_session()

    def __str__(self) -> str:
        payload = {k: v for k, v in self.__dict__.items() if k != "store"}
        return str(payload)
