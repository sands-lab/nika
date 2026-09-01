from __future__ import annotations

import pytest
import json
import tempfile
from pathlib import Path
from nika.utils.session_index import SessionIndex
from nika.utils.session_store import SessionStore

RUN_FILENAME = "run.json"
GROUND_TRUTH_FILENAME = "ground_truth.json"
EVAL_METRICS_FILENAME = "eval_metrics.json"


class SessionIndexTestCase:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir()
        self.db_path = self.root / "sessions.db"
        self.results_dir = self.root / "results"
        self.results_dir.mkdir()
        self.index = SessionIndex(self.db_path)
        self.store = SessionStore(self.sessions_dir, self.db_path)
        yield
        self.tmp.cleanup()

    def _create_session(self, session_id: str = "20260101-120000-abc123") -> None:
        self.store.create_session(
            {
                "session_id": session_id,
                "lab_name": f"bgp__{session_id[-6:]}",
                "scenario_name": "bgp",
                "scenario_topo_size": "s",
                "session_dir": str(self.results_dir / session_id),
                "status": "running",
            }
        )

    def test_failure_injection_increments_count(self) -> None:
        self._create_session()
        self.store.create_failure_injection(
            {
                "session_id": "20260101-120000-abc123",
                "problem_name": "link_down",
                "scenario_name": "bgp",
                "lab_name": "bgp__abc123",
            }
        )
        row = self.index.get_row("20260101-120000-abc123")
        assert row is not None
        assert row["failure_count"] == 1

    def test_purge_and_truncate(self) -> None:
        self._create_session()
        self.index.purge("20260101-120000-abc123")
        assert self.index.get_row("20260101-120000-abc123") is None
        self._create_session("20260102-120000-def456")
        self.index.truncate()
        assert len(self.index.list_sessions(running_only=False)) == 0

    def test_rebuild_from_results(self) -> None:
        session_id = "20260103-120000-fff999"
        session_dir = self.results_dir / session_id
        session_dir.mkdir()
        run_meta = {
            "session_id": session_id,
            "status": "finished",
            "scenario_name": "bgp",
            "lab_name": "bgp__fff999",
            "agent_type": "mock",
            "problem_names": ["link_down"],
        }
        (session_dir / RUN_FILENAME).write_text(json.dumps(run_meta), encoding="utf-8")
        (session_dir / GROUND_TRUTH_FILENAME).write_text(
            json.dumps(
                {
                    "is_anomaly": True,
                    "root_causes": [
                        {
                            "resource_id": "node/pc1",
                            "fault_type": "link_down",
                        }
                    ],
                    "failure_domain": "link_interface",
                }
            ),
            encoding="utf-8",
        )
        (session_dir / EVAL_METRICS_FILENAME).write_text(
            json.dumps({"detection_score": 1.0, "localization_f1": 1.0, "rca_f1": 1.0}),
            encoding="utf-8",
        )
        count = self.index.rebuild_from_results(self.results_dir)
        assert count == 1
        row = self.index.get_row(session_id)
        assert row is not None
        assert row["status"] == "finished"
        assert row["agent_type"] == "mock"
        assert row["problem_names"] == ["link_down"]
        assert row["failure_domain"] == "link_interface"
        assert row["detection_score"] == 1.0
        assert row["failure_count"] == 1
