"""Tests for trial benchmark runs (cases × K trials under result_dir)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nika.workflows.benchmark.trials import (
    case_key_for_row,
    expand_trials,
    is_valid_trial,
    merge_run_config,
    scan_trials,
    trial_dir,
    trial_dirname,
)
from nika.workflows.benchmark.release import (
    JOB_FILENAME,
    RUN_CONFIG_FILENAME,
    freeze_release,
    load_run_config,
)
from nika.workflows.benchmark.resume import benchmark_row_fingerprint
from nika.workflows.benchmark.run import (
    run_benchmark_from_release,
    run_benchmark_trials,
)
from tests.support.prerequisites import docker_available


ROW_A = {
    "scenario": "simple_bgp",
    "problem": "link_down",
    "topo_size": "",
    "inject": {"host_name": "pc1", "intf_name": "eth0"},
}
ROW_B = {
    "scenario": "simple_bgp",
    "problem": "link_flap",
    "topo_size": "",
    "inject": {"host_name": "pc1", "intf_name": "eth0"},
}


def _write_valid_trial(
    path: Path,
    *,
    outcome: str = "success",
    session_id: str | None = None,
    fingerprint: str | None = None,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    sid = session_id or path.name
    run_meta = {
        "session_id": sid,
        "status": "finished",
        "outcome": outcome,
        "benchmark_fingerprint": fingerprint or "fp",
    }
    (path / "run.json").write_text(json.dumps(run_meta), encoding="utf-8")
    (path / "ground_truth.json").write_text("{}", encoding="utf-8")
    (path / "messages.jsonl").write_text("", encoding="utf-8")
    (path / "eval_metrics.json").write_text("{}", encoding="utf-8")
    if outcome == "success":
        (path / "submission.json").write_text("{}", encoding="utf-8")


def _mini_cases_yaml(path: Path, rows: list[dict] | None = None) -> Path:
    payload = {"seed": 42, "cases": rows or [ROW_A, ROW_B]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


class TestTrialHelpers:
    def test_case_key_and_trial_dirname_are_stable(self) -> None:
        key = case_key_for_row(ROW_A)
        assert key.startswith("simple_bgp__link_down__")
        assert len(key.rsplit("__", 1)[-1]) == 8
        assert trial_dirname(key, 1) == f"{key}__t01"
        assert trial_dirname(key, 12) == f"{key}__t12"
        assert case_key_for_row(ROW_A) == key

    def test_expand_trials(self) -> None:
        trials = expand_trials([ROW_A, ROW_B], n_trials=2)
        assert len(trials) == 4
        assert [s.trial_index for s in trials] == [1, 2, 1, 2]
        assert trials[0].case_key == case_key_for_row(ROW_A)
        assert trials[0].trial_id == trial_dirname(trials[0].case_key, 1)
        assert trials[1].trial_id == trial_dirname(trials[0].case_key, 2)

    def test_is_valid_trial_requires_artifacts(self, tmp_path: Path) -> None:
        path = tmp_path / "t01"
        path.mkdir()
        (path / "run.json").write_text(
            json.dumps({"status": "finished", "outcome": "success"}),
            encoding="utf-8",
        )
        assert not is_valid_trial(path)
        _write_valid_trial(path, outcome="success")
        assert is_valid_trial(path)

    def test_agent_failed_without_submission_is_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "t_fail"
        _write_valid_trial(path, outcome="agent_failed")
        assert not (path / "submission.json").exists()
        assert is_valid_trial(path)

    def test_scan_skips_complete_and_cleans_incomplete(self, tmp_path: Path) -> None:
        trials = expand_trials([ROW_A, ROW_B], n_trials=1)
        done = trial_dir(tmp_path, trials[0].case_key, 1)
        _write_valid_trial(done, outcome="success")

        incomplete = trial_dir(tmp_path, trials[1].case_key, 1)
        incomplete.mkdir(parents=True)
        (incomplete / "run.json").write_text(
            json.dumps({"session_id": incomplete.name, "status": "running"}),
            encoding="utf-8",
        )

        _root, pending = scan_trials(
            trials=trials, result_dir=tmp_path, resume=True
        )
        assert pending == [1]
        assert not incomplete.exists()
        assert done.exists()

    def test_scan_never_deletes_agent_failed(self, tmp_path: Path) -> None:
        trials = expand_trials([ROW_A], n_trials=1)
        failed = trial_dir(tmp_path, trials[0].case_key, 1)
        _write_valid_trial(failed, outcome="agent_failed")
        _root, pending = scan_trials(
            trials=trials, result_dir=tmp_path, resume=True
        )
        assert pending == []
        assert failed.exists()
        assert is_valid_trial(failed)

    def test_merge_run_config_keeps_run_id(self) -> None:
        proposed = {
            "benchmark_id": "nika-bench",
            "version": "mini",
            "benchmark_digest": "abc",
            "split": "dev",
            "cases_sha256": "0" * 64,
            "agent_type": "mock",
            "model": "mock-v1",
            "llm_provider": None,
            "max_steps": None,
            "n_trials": 2,
            "case_timeout_sec": 2400,
            "official": True,
            "run_id": "newid",
            "job_id": "newid",
        }
        first = merge_run_config(existing=None, proposed=proposed)
        assert first["run_id"] == "newid"
        assert "created_at" in first

        proposed2 = dict(proposed)
        proposed2["run_id"] = "other"
        proposed2["job_id"] = "other"
        second = merge_run_config(existing=first, proposed=proposed2)
        assert second["run_id"] == "newid"
        assert second["updated_at"] != first["created_at"] or True

    def test_merge_run_config_rejects_mismatch(self) -> None:
        existing = {
            "benchmark_id": "nika-bench",
            "version": "mini",
            "benchmark_digest": "abc",
            "split": "dev",
            "cases_sha256": "0" * 64,
            "agent_type": "mock",
            "model": "mock-v1",
            "llm_provider": None,
            "max_steps": None,
            "n_trials": 2,
            "case_timeout_sec": 2400,
            "official": True,
            "run_id": "keep",
            "job_id": "keep",
            "created_at": "t0",
            "updated_at": "t0",
        }
        proposed = dict(existing)
        proposed["n_trials"] = 3
        with pytest.raises(ValueError, match="n_trials"):
            merge_run_config(existing=existing, proposed=proposed)


class TestTrialOrchestration:
    def test_cardinality_and_isolation(self, tmp_path: Path) -> None:
        cases = _mini_cases_yaml(tmp_path / "cases.yaml")
        run_a = tmp_path / "run_a"
        run_b = tmp_path / "run_b"
        written: list[str] = []

        def fake_trial(trial, **kwargs):
            result_dir = Path(kwargs["result_dir"])
            path = trial_dir(result_dir, trial.case_key, trial.trial_index)
            _write_valid_trial(path, outcome="success", session_id=trial.trial_id)
            written.append(str(path))

        with patch(
            "nika.workflows.benchmark.run._run_trial", side_effect=fake_trial
        ):
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=2,
                result_dir=str(run_a),
            )
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=2,
                result_dir=str(run_b),
            )

        trials_a = sorted(p.name for p in (run_a / "trials").iterdir())
        trials_b = sorted(p.name for p in (run_b / "trials").iterdir())
        assert len(trials_a) == 4
        assert trials_a == trials_b
        assert all(is_valid_trial(run_a / "trials" / name) for name in trials_a)

        # Isolation: completing run_a does not skip work in an empty sibling dir.
        assert (run_a / "trials").is_dir()
        empty = tmp_path / "run_empty"
        with patch(
            "nika.workflows.benchmark.run._run_trial", side_effect=fake_trial
        ) as mocked:
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=2,
                result_dir=str(empty),
            )
            assert mocked.call_count == 4

    def test_resume_skips_completed_trials(self, tmp_path: Path) -> None:
        cases = _mini_cases_yaml(tmp_path / "cases.yaml")
        result_dir = tmp_path / "run"
        trials = expand_trials([ROW_A, ROW_B], n_trials=2)
        # Pre-complete first two trials.
        for trial in trials[:2]:
            _write_valid_trial(
                trial_dir(result_dir, trial.case_key, trial.trial_index),
                outcome="success",
                session_id=trial.trial_id,
            )

        calls: list[str] = []

        def fake_trial(trial, **kwargs):
            calls.append(trial.trial_id)
            path = trial_dir(
                Path(kwargs["result_dir"]), trial.case_key, trial.trial_index
            )
            _write_valid_trial(path, outcome="success", session_id=trial.trial_id)

        with patch(
            "nika.workflows.benchmark.run._run_trial", side_effect=fake_trial
        ):
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=2,
                result_dir=str(result_dir),
                resume=True,
            )

        assert calls == [trials[2].trial_id, trials[3].trial_id]
        assert len(list((result_dir / "trials").iterdir())) == 4

    def test_retry_does_not_overwrite_agent_failed(self, tmp_path: Path) -> None:
        cases = _mini_cases_yaml(tmp_path / "cases.yaml", rows=[ROW_A])
        result_dir = tmp_path / "run"
        trials = expand_trials([ROW_A], n_trials=1)
        failed = trial_dir(result_dir, trials[0].case_key, 1)
        _write_valid_trial(failed, outcome="agent_failed", session_id=trials[0].trial_id)
        original = (failed / "run.json").read_text(encoding="utf-8")

        with patch("nika.workflows.benchmark.run._run_trial") as mocked:
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=1,
                result_dir=str(result_dir),
                resume=True,
                retry_passes=2,
            )
            mocked.assert_not_called()

        assert (failed / "run.json").read_text(encoding="utf-8") == original

    def test_parallel_and_serial_same_trial_set(self, tmp_path: Path) -> None:
        cases = _mini_cases_yaml(tmp_path / "cases.yaml")
        serial_dir = tmp_path / "serial"
        parallel_dir = tmp_path / "parallel"

        def fake_trial(trial, **kwargs):
            path = trial_dir(
                Path(kwargs["result_dir"]), trial.case_key, trial.trial_index
            )
            _write_valid_trial(path, outcome="success", session_id=trial.trial_id)

        # Patch the timeout wrapper so parallel batches do not spawn processes
        # (spawn cannot pickle MagicMock / local side_effect targets).
        with patch(
            "nika.workflows.benchmark.run._run_trial_with_timeout",
            side_effect=fake_trial,
        ):
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=2,
                batch_size=1,
                result_dir=str(serial_dir),
            )
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=2,
                batch_size=2,
                result_dir=str(parallel_dir),
            )

        assert sorted(p.name for p in (serial_dir / "trials").iterdir()) == sorted(
            p.name for p in (parallel_dir / "trials").iterdir()
        )


class TestAgentFailedFinalization:
    def test_agent_failure_keeps_counted_trial(self, tmp_path: Path) -> None:
        result_dir = tmp_path / "run"
        trials = expand_trials([ROW_A], n_trials=1)
        trial = trials[0]
        session_path = trial_dir(result_dir, trial.case_key, 1)

        def fake_start_net_env(*args, **kwargs):
            sid = kwargs["session_id"]
            sdir = Path(kwargs["session_dir"])
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / "run.json").write_text(
                json.dumps(
                    {
                        "session_id": sid,
                        "status": "running",
                        "session_dir": str(sdir),
                        "scenario_name": "simple_bgp",
                    }
                ),
                encoding="utf-8",
            )
            from nika.utils.session_store import SessionStore

            SessionStore().create_session(
                {
                    "session_id": sid,
                    "lab_name": "lab",
                    "scenario_name": "simple_bgp",
                    "scenario_topo_size": None,
                    "scenario_params": {},
                    "session_dir": str(sdir),
                    "status": "running",
                    "backend": "kathara",
                }
            )
            return sid

        def fake_inject(**kwargs):
            session_id = kwargs["session_id"]
            from nika.utils.session_store import SessionStore

            sdir = Path(SessionStore().get_session(session_id)["session_dir"])
            (sdir / "ground_truth.json").write_text(
                json.dumps(
                    {
                        "is_anomaly": True,
                        "faulty_devices": ["pc1"],
                        "root_cause_category": "link_failure",
                        "root_cause_name": ["link_down"],
                    }
                ),
                encoding="utf-8",
            )

        with (
            patch(
                "nika.workflows.benchmark.run.start_net_env",
                side_effect=fake_start_net_env,
            ),
            patch(
                "nika.workflows.benchmark.run.inject_failure",
                side_effect=fake_inject,
            ),
            patch(
                "nika.workflows.benchmark.run.start_agent",
                side_effect=RuntimeError("agent boom"),
            ),
            patch("nika.workflows.benchmark.run.close_session"),
            patch(
                "nika.workflows.benchmark.run._stamp_release_meta",
            ),
            patch(
                "nika.workflows.benchmark.run._stamp_trial_meta",
            ),
            patch(
                "nika.workflows.benchmark.run.Session.load_running_session",
                side_effect=lambda *a, **k: type(
                    "S",
                    (),
                    {
                        "update_session": lambda self, key, value: None,
                    },
                )(),
            ),
        ):
            from nika.workflows.benchmark.run import run_single_case

            sid, sdir = run_single_case(
                problem="link_down",
                scenario="simple_bgp",
                topo_size="",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                inject_params=ROW_A["inject"],
                result_dir=str(result_dir),
                trial_id=trial.trial_id,
                trial_index=trial.trial_index,
                case_key=trial.case_key,
            )

        assert sid == trial.trial_id
        assert sdir == session_path
        assert is_valid_trial(session_path)
        run_meta = json.loads((session_path / "run.json").read_text(encoding="utf-8"))
        assert run_meta["outcome"] == "agent_failed"
        assert run_meta["status"] == "finished"
        assert (session_path / "ground_truth.json").is_file()
        assert (session_path / "messages.jsonl").is_file()
        assert (session_path / "eval_metrics.json").is_file()
        assert not (session_path / "submission.json").exists()


class TestReleaseRunMetadata:
    def test_release_writes_stable_run_config(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml", rows=[ROW_A])
        release = freeze_release(
            version="mini-release",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini-release",
        )
        result_dir = tmp_path / "results"

        with (
            patch("nika.workflows.benchmark.run.preflight_release"),
            patch("nika.workflows.benchmark.run.run_benchmark_trials"),
            patch(
                "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
                tmp_path / "benchmark_runs",
            ),
        ):
            run_benchmark_from_release(
                release_ref="mini-release",
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=10,
                result_dir=str(result_dir),
                case_timeout=2400,
                check_images=False,
                release=release,
            )
            first = load_run_config(result_dir)
            assert first is not None
            run_id = first["run_id"]
            assert first["n_trials"] == release.n_trials
            assert (result_dir / RUN_CONFIG_FILENAME).is_file()
            assert (result_dir / JOB_FILENAME).is_file()

            run_benchmark_from_release(
                release_ref="mini-release",
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=10,
                result_dir=str(result_dir),
                case_timeout=2400,
                check_images=False,
                release=release,
            )
            second = load_run_config(result_dir)
            assert second is not None
            assert second["run_id"] == run_id

            # Resume refuses a different agent (identity mismatch).
            with pytest.raises(ValueError, match="agent_type"):
                run_benchmark_from_release(
                    release_ref="mini-release",
                    split="dev",
                    agent_type="other.mock",
                    llm_provider=None,
                    model="mock-v1",
                    max_steps=10,
                    result_dir=str(result_dir),
                    case_timeout=2400,
                    check_images=False,
                    release=release,
                )

    def test_fingerprint_included_in_case_key(self) -> None:
        other = {
            **ROW_A,
            "inject": {"host_name": "pc2", "intf_name": "eth0"},
        }
        assert case_key_for_row(ROW_A) != case_key_for_row(other)
        assert benchmark_row_fingerprint(ROW_A) != benchmark_row_fingerprint(other)

    def test_runtime_progress_tracks_completed_trials(self, tmp_path: Path) -> None:
        cases = _mini_cases_yaml(tmp_path / "cases.yaml")
        result_dir = tmp_path / "run"
        runs_dir = tmp_path / "benchmark_runs"
        release_meta = {
            "run_id": "progress-test-run",
            "job_id": "progress-test-run",
            "benchmark_id": "nika-bench",
            "version": "0.1.0",
            "agent_type": "mock",
            "model": "mock-v1",
        }

        def fake_trial(trial, **kwargs):
            path = trial_dir(
                Path(kwargs["result_dir"]), trial.case_key, trial.trial_index
            )
            _write_valid_trial(path, outcome="success", session_id=trial.trial_id)

        with (
            patch(
                "nika.workflows.benchmark.run._run_trial",
                side_effect=fake_trial,
            ),
            patch(
                "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
                runs_dir,
            ),
        ):
            run_benchmark_trials(
                benchmark_file=str(cases),
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                n_trials=2,
                result_dir=str(result_dir),
                release_meta=release_meta,
            )

        progress_path = runs_dir / "progress-test-run.json"
        assert progress_path.is_file()
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        assert progress["status"] == "finished"
        assert progress["total_trials"] == 4
        assert progress["completed_trials"] == 4
        assert progress["pending_trials"] == 0
        assert progress["result_dir"] == str(result_dir.resolve())
        assert progress["agent_type"] == "mock"
        assert progress["model"] == "mock-v1"


@pytest.mark.skipif(not docker_available(), reason="Docker required for release run E2E")
class TestReleaseRunE2E:
    """Real Kathara + mock agent through ``run_benchmark_from_release`` (1 case × 2 trials)."""

    def test_mini_release_run_flow(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml", rows=[ROW_A])
        release = freeze_release(
            version="release-e2e",
            source_cases=source,
            out_dir=tmp_path / "releases" / "release-e2e",
        )
        # Two trials prove trial expansion / distinct dirs without running a full suite.
        release = replace(release, defaults={**release.defaults, "n_trials": 2})
        assert release.case_count == 1
        assert release.n_trials == 2

        result_dir = tmp_path / "release-e2e-run"
        runs_dir = tmp_path / "benchmark_runs"

        with patch(
            "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
            runs_dir,
        ):
            run_benchmark_from_release(
                release_ref="release-e2e",
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=20,
                result_dir=str(result_dir),
                case_timeout=600,
                check_images=False,
                release=release,
            )

        job = load_run_config(result_dir)
        assert job is not None
        assert job["n_trials"] == 2
        assert job["official"] is True
        assert (result_dir / RUN_CONFIG_FILENAME).is_file()
        assert (result_dir / JOB_FILENAME).is_file()
        assert (result_dir / "RELEASE.lock.json").is_file()

        row = release.cases[0]
        key = case_key_for_row(row)
        trial_paths = [trial_dir(result_dir, key, idx) for idx in (1, 2)]
        assert all(is_valid_trial(path) for path in trial_paths)
        trial_ids = set()
        for idx, trial_path in enumerate(trial_paths, start=1):
            trial_meta = json.loads((trial_path / "run.json").read_text(encoding="utf-8"))
            assert trial_meta["status"] == "finished"
            assert trial_meta["outcome"] in {"success", "agent_failed"}
            assert trial_meta.get("trial_index") == idx
            assert trial_meta.get("trial_id") == trial_dirname(key, idx)
            assert trial_meta.get("benchmark_run_id") == job["run_id"]
            assert trial_meta.get("benchmark_id") == "nika-bench"
            trial_ids.add(trial_meta["trial_id"])
        assert trial_ids == {trial_dirname(key, 1), trial_dirname(key, 2)}

        progress_path = runs_dir / f"{job['run_id']}.json"
        assert progress_path.is_file()
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        assert progress["status"] == "finished"
        assert progress["total_trials"] == 2
        assert progress["completed_trials"] == 2
        assert progress["pending_trials"] == 0
        assert progress["result_dir"] == str(result_dir.resolve())

        # Resume must see both counted trials and not re-execute.
        with (
            patch(
                "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
                runs_dir,
            ),
            patch("nika.workflows.benchmark.run._run_trial") as mocked_trial,
        ):
            run_benchmark_from_release(
                release_ref="release-e2e",
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=20,
                result_dir=str(result_dir),
                case_timeout=600,
                check_images=False,
                release=release,
            )
            assert mocked_trial.call_count == 0
