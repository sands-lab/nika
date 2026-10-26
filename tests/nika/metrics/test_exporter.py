"""Session and benchmark-run attribution of the NIKA metrics exporter."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nika.metrics.exporter import SessionCollector
from nika.utils.session_store import SessionStore
from nika.workflows.benchmark.run_progress import is_run_live, write_progress

pytestmark = pytest.mark.unit

STACK_DIR = Path(__file__).resolve().parents[3] / "src" / "nika" / "metrics" / "stack"
_DEAD_PID = 2**22 + 1  # above the default pid_max, so never a live process


def _samples(collector: SessionCollector) -> dict[str, list]:
    out: dict[str, list] = {}
    for family in collector.collect():
        out[family.name] = [(s.labels, s.value) for s in family.samples]
    return out


def _session(store: SessionStore, session_id: str, session_dir: Path, **extra) -> None:
    store.create_session(
        {
            "session_id": session_id,
            "lab_name": f"lab_{session_id}",
            "scenario_name": "dc_clos",
            "backend": "kathara",
            "session_dir": str(session_dir),
            **extra,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions", tmp_path / "sessions.db")


def test_sessions_and_containers_are_attributed_to_their_run(
    tmp_path: Path, store: SessionStore
) -> None:
    runs_dir = tmp_path / "benchmark_runs"
    result_dir = tmp_path / "results" / "job"
    write_progress(
        "run1",
        result_dir=result_dir,
        total_trials=3,
        completed_trials=1,
        pending_trials=2,
        agent_type="mock",
        runs_dir=runs_dir,
    )
    _session(store, "trial", result_dir / "trials" / "case__t01")
    _session(store, "adhoc", tmp_path / "results" / "adhoc")
    _session(store, "vm", result_dir / "trials" / "case__t02", remote=True)

    def containers(session_id, *, store):
        if session_id == "adhoc":
            raise RuntimeError("lab gone")
        return session_id, "lab", [{"name": "r1", "container_name": f"c_{session_id}"}]

    with patch(
        "nika.workflows.session.containers.list_session_containers",
        side_effect=containers,
    ) as listed:
        samples = _samples(SessionCollector(store, runs_dir=runs_dir))

    # A remote or Firecracker lab is out of this host's sight: not listed.
    assert sorted(call.args[0] for call in listed.call_args_list) == ["adhoc", "trial"]
    run_of = {
        labels["session_id"]: labels["run_id"]
        for labels, _ in samples["nika_session_info"]
    }
    assert run_of == {"trial": "run1", "adhoc": "", "vm": "run1"}
    assert samples["nika_container_info"] == [
        (
            {
                "session_id": "trial",
                "run_id": "run1",
                "lab_name": "lab_trial",
                "backend": "kathara",
                "scenario": "dc_clos",
                "node": "r1",
                "name": "c_trial",
            },
            1,
        )
    ]
    assert samples["nika_session_discovery_errors"] == [({}, 1)]
    trials = {
        labels["state"]: value
        for labels, value in samples["nika_benchmark_trials"]
        if labels["run_id"] == "run1"
    }
    assert trials == {"total": 3, "completed": 1, "pending": 2, "running": 2}
    (info,) = samples["nika_benchmark_run_info"]
    assert info[0]["status"] == "running"
    assert info[0]["agent_type"] == "mock"


def test_stamped_run_id_applies_outside_known_result_dirs(
    tmp_path: Path, store: SessionStore
) -> None:
    _session(store, "s1", tmp_path / "elsewhere", benchmark_run_id="rel1", remote=True)

    samples = _samples(SessionCollector(store, runs_dir=tmp_path / "none"))

    assert samples["nika_session_info"][0][0]["run_id"] == "rel1"


def test_runs_whose_orchestrator_died_are_stopped_then_dropped(
    tmp_path: Path, store: SessionStore
) -> None:
    runs_dir = tmp_path / "benchmark_runs"
    for run_id in ("crashed", "old"):
        write_progress(run_id, result_dir=tmp_path / run_id, runs_dir=runs_dir)
    old = runs_dir / "old.json"
    for path in runs_dir.glob("*.json"):
        doc = json.loads(path.read_text())
        doc["pid"] = _DEAD_PID
        if path == old:
            doc["updated_at"] = (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat()
        path.write_text(json.dumps(doc))

    samples = _samples(SessionCollector(store, runs_dir=runs_dir))

    assert [labels for labels, _ in samples["nika_benchmark_run_info"]] == [
        {
            "run_id": "crashed",
            "benchmark_id": "",
            "version": "",
            "agent_type": "",
            "model": "",
            "status": "stopped",
        }
    ]


def test_run_liveness_follows_status_and_process() -> None:
    assert is_run_live({"status": "running", "pid": os.getpid()})
    assert not is_run_live({"status": "finished", "pid": os.getpid()})
    assert not is_run_live({"status": "running", "pid": _DEAD_PID})
    assert not is_run_live({"status": "running"})


def test_dashboard_queries_only_published_series() -> None:
    """Every series the dashboard reads is recorded or exported by NIKA."""
    rules = yaml.safe_load((STACK_DIR / "rules.yml").read_text())
    recorded = {rule["record"] for group in rules["groups"] for rule in group["rules"]}
    exported = {family.name for family in SessionCollector(_EmptyStore()).collect()}
    dashboard = (
        STACK_DIR / "grafana" / "dashboards" / "nika-benchmark.json"
    ).read_text()
    used = set(re.findall(r"\bnika[:_][a-z_:]+", dashboard)) - {"nika-benchmark"}
    queried = {name for name in used if name.startswith(("nika:", "nika_"))}

    assert queried
    assert queried <= recorded | exported


class _EmptyStore:
    def list_running_sessions(self) -> list:
        return []


def test_remote_write_pushes_only_nika_series_with_token_file(
    tmp_path: Path, monkeypatch, request: pytest.FixtureRequest
) -> None:
    from nika.metrics import stack
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig

    request.addfinalizer(reset_run_config)
    monkeypatch.setattr(stack, "PROMETHEUS_DIR", tmp_path / "prometheus")
    set_run_config(
        RunConfig.model_validate(
            {
                "nika": {
                    "metrics": {
                        "scrape_interval": "30s",
                        "remote_write": {
                            "url": "https://example.test/api/prom/push",
                            "username": "123",
                        },
                    }
                }
            }
        )
    )
    monkeypatch.delenv(stack.REMOTE_WRITE_TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError, match=stack.REMOTE_WRITE_TOKEN_ENV):
        stack.render_prometheus_config()

    monkeypatch.setenv(stack.REMOTE_WRITE_TOKEN_ENV, "s3cret")
    stack.render_prometheus_config()

    rendered = (tmp_path / "prometheus" / "prometheus.yml").read_text()
    config = yaml.safe_load(rendered)
    assert "s3cret" not in rendered
    assert config["global"]["scrape_interval"] == "30s"
    (remote,) = config["remote_write"]
    assert remote["basic_auth"]["username"] == "123"
    assert remote["write_relabel_configs"][0]["regex"] == "nika:.*|nika_.*"
    token = tmp_path / "prometheus" / "remote_write_token"
    assert token.read_text() == "s3cret"
    assert token.stat().st_mode & 0o007 == 0
    rules = (tmp_path / "prometheus" / "rules.yml").read_text()
    assert "[120s]" in rules and "$rate_window" not in rules
