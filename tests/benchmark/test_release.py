"""Unit tests for frozen ``nika-bench`` releases and Dev/Test splits."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nika.workflows.benchmark.release import (
    JOB_FILENAME,
    ReleaseError,
    freeze_release,
    load_release,
    load_release_from_dir,
    verify_engine,
    preflight_release,
    verify_dev_test_isolation,
)
from nika.workflows.benchmark.run import run_benchmark_from_release

pytestmark = pytest.mark.contract


def _mini_cases_yaml(path: Path) -> Path:
    payload = {
        "seed": 42,
        "cases": [
            {
                "scenario": "dc_clos",
                "topo_size": "s",
                "problem": "link_down",
                "inject": {"host_name": "client_0", "intf_name": "eth0"},
            }
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


class TestReleaseRefs:
    def test_sha256_ref_rejected(self) -> None:
        with pytest.raises(ReleaseError, match="Digest-based"):
            load_release(
                "nika-bench@sha256:226d3209e3c3c46aade8c37ecd989642ae73692f0d9f149995954553e41474d1",
                split="dev",
            )


def test_published_0_2_0_release_preflight() -> None:
    """The documented release must load and pass its complete contract gate."""
    release = load_release("0.2.0", split="test")

    assert release.case_count == 85
    assert release.n_trials == 3
    preflight_release(release, check_images=False)


def test_timed_release_faults_cover_the_default_trial_budget() -> None:
    """Worker expiry must not invalidate a legitimate long benchmark trial."""
    from nika.problems.registry import get_problem_class
    from nika.run_config.schema import BenchmarkSettings

    timed_faults = {
        "sender_resource_contention",
        "receiver_resource_contention",
        "tcp_syn_flood_attack",
    }
    timeout = BenchmarkSettings.model_fields["case_timeout_sec"].default
    for split in ("dev", "test"):
        for row in load_release("0.2.0", split=split).cases:
            if row["problem"] in timed_faults:
                cls = get_problem_class(row["problem"])
                params = cls.Params.model_validate(row["inject"])
                assert params.duration > timeout, (split, row)


class TestManifestContracts:
    """Contract A: refuse versions and task files this NIKA cannot vouch for."""

    def _release_dir(self, tmp_path: Path, **fields) -> Path:
        release = freeze_release(
            version="mini",
            source_cases=_mini_cases_yaml(tmp_path / "cases_src.yaml"),
            out_dir=tmp_path / "releases" / "mini",
        )
        path = release.root / "RELEASE.yaml"
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest.update(fields)
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        return release.root

    def test_supported_versions_and_matching_hash_load(self, tmp_path: Path) -> None:
        import hashlib

        root = self._release_dir(tmp_path)
        digest = hashlib.sha256((root / "dev.yaml").read_bytes()).hexdigest()
        manifest = yaml.safe_load((root / "RELEASE.yaml").read_text(encoding="utf-8"))
        manifest["manifest_version"] = 1
        manifest["splits"]["dev"]["sha256"] = digest
        (root / "RELEASE.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

        assert load_release_from_dir(root, split="dev").case_count == 1

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"manifest_version": 99}, "Release manifest version 99"),
            (
                {"engine": {"tag": "v9.0.0", "protocol": 99, "results_schema": 1}},
                "Lifecycle protocol version 99",
            ),
            (
                {"engine": {"tag": "v9.0.0", "protocol": 1, "results_schema": 99}},
                "Results schema version 99",
            ),
            ({"engine": {"tag": "v9.0.0"}}, "Lifecycle protocol version must be"),
        ],
    )
    def test_unsupported_contract_version_is_refused(
        self, tmp_path: Path, fields: dict, message: str
    ) -> None:
        root = self._release_dir(tmp_path, **fields)
        with pytest.raises(ReleaseError, match=message):
            load_release_from_dir(root, split="dev")

    def test_drifted_task_file_is_refused(self, tmp_path: Path) -> None:
        root = self._release_dir(tmp_path)
        manifest = yaml.safe_load((root / "RELEASE.yaml").read_text(encoding="utf-8"))
        manifest["splits"]["dev"]["sha256"] = "0" * 64
        (root / "RELEASE.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

        with pytest.raises(ReleaseError, match="does not match the sha256"):
            load_release_from_dir(root, split="dev")

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"protocol_versions": [99]}, "lifecycle protocol versions"),
            ({"release_version": "9.9.9"}, "not nika-bench@mini"),
            ({"task_file_sha256": {"dev.yaml": "0" * 64}}, "does not match"),
        ],
    )
    def test_mismatched_engine_is_refused(
        self, tmp_path: Path, change: dict, message: str
    ) -> None:
        import hashlib

        from nika.benchmark_engine.protocol import EngineDescription

        root = self._release_dir(tmp_path)
        digest = hashlib.sha256((root / "dev.yaml").read_bytes()).hexdigest()
        manifest = yaml.safe_load((root / "RELEASE.yaml").read_text(encoding="utf-8"))
        manifest["splits"]["dev"]["sha256"] = digest
        (root / "RELEASE.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        release = load_release_from_dir(root, split="dev")
        fields = {
            "protocol_versions": [1],
            "release_id": release.id,
            "release_version": release.version,
            "task_file_sha256": {"dev.yaml": digest},
        }

        verify_engine(release, EngineDescription(**fields))
        with pytest.raises(ReleaseError, match=message):
            verify_engine(release, EngineDescription(**{**fields, **change}))

    def test_pinned_engine_release_does_not_run_in_process(
        self, tmp_path: Path
    ) -> None:
        root = self._release_dir(
            tmp_path,
            engine={"tag": "v9.0.0", "protocol": 1, "results_schema": 1},
        )
        release = load_release_from_dir(root, split="dev")

        with pytest.raises(ReleaseError, match="pinned benchmark engine"):
            run_benchmark_from_release(
                release_ref="mini",
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model=None,
                max_steps=None,
                release=release,
                result_dir=str(tmp_path / "results"),
                check_images=False,
            )


class TestFreezeRelease:
    def test_freeze_writes_versioned_manifest(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        assert release.scoring["id"] == "rule-based"
        assert release.cases[0]["root_causes"]
        assert release.version == "mini"
        assert release.case_count == 1
        manifest = yaml.safe_load(
            (release.root / "RELEASE.yaml").read_text(encoding="utf-8")
        )
        assert "benchmark_digest" not in manifest
        assert "scenario_problem_pin" not in manifest
        assert "cases_sha256" not in manifest["splits"]["dev"]
        assert manifest["splits"]["dev"]["case_count"] == 1

    def test_semantic_context_overlap_is_rejected(self) -> None:
        base = {
            "scenario": "dc_clos",
            "topo_size": "s",
            "problem": "link_down",
        }
        dev = [{**base, "inject": {"host_name": "pc_0_0", "intf_name": "eth0"}}]
        test = [{**base, "inject": {"host_name": "pc_0_1", "intf_name": "eth0"}}]
        with pytest.raises(ReleaseError, match="semantic isolation"):
            verify_dev_test_isolation(dev_cases=dev, test_cases=test)

    def test_preflight_missing_scenario(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        with patch(
            "nika.workflows.benchmark.release.list_all_net_envs",
            return_value={},
        ):
            with pytest.raises(ReleaseError, match="Missing scenarios"):
                preflight_release(release, check_images=False)

    def test_preflight_missing_problem(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        with patch(
            "nika.workflows.benchmark.release.list_avail_problem_instances",
            return_value={},
        ):
            with pytest.raises(ReleaseError, match="Missing problems"):
                preflight_release(release, check_images=False)

    def test_preflight_ensures_images(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        with patch(
            "nika.workflows.benchmark.release.ensure_nika_docker_images",
        ) as ensure:
            preflight_release(release, check_images=True)
            ensure.assert_called_once()
            assert ensure.call_args.args[0] == list(
                release.images.get("required") or []
            )

    def test_preflight_image_ensure_failure(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        with patch(
            "nika.workflows.benchmark.release.ensure_nika_docker_images",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(ReleaseError, match="Required Docker images"):
                preflight_release(release, check_images=True)


class TestReleaseRunMetadata:
    def test_run_from_release_writes_job_metadata(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        result_dir = tmp_path / "results"
        runs_dir = tmp_path / "benchmark_runs"
        written: dict[str, dict] = {}

        def _capture_written(*_args, **_kwargs) -> list[str]:
            # Run config and progress exist (post-confirm) before trials start.
            job_doc = json.loads((result_dir / JOB_FILENAME).read_text("utf-8"))
            progress_doc = runs_dir / f"{job_doc['run_id']}.json"
            written["progress"] = json.loads(progress_doc.read_text("utf-8"))
            return []

        with (
            patch(
                "nika.workflows.benchmark.release.RELEASES_DIR", tmp_path / "releases"
            ),
            patch(
                "nika.workflows.benchmark.run.preflight_release",
                return_value=None,
            ),
            patch(
                "nika.workflows.benchmark.run._run_trials_batch",
                side_effect=_capture_written,
            ) as run_trials,
            patch(
                "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
                runs_dir,
            ),
        ):
            run_benchmark_from_release(
                release_ref="mini",
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                result_dir=str(result_dir),
                case_timeout=2400,
                check_images=False,
                release=release,
            )

        job_path = result_dir / JOB_FILENAME
        assert job_path.is_file()
        assert (result_dir / "run.json").is_file()
        job = json.loads(job_path.read_text(encoding="utf-8"))
        assert job["benchmark_id"] == "nika-bench"
        assert job["version"] == "mini"
        assert job["split"] == "dev"
        assert "benchmark_digest" not in job
        assert "cases_sha256" not in job
        assert job["case_count"] == 1
        assert job["n_trials"] == 3
        assert job["run_id"] == job["job_id"]
        assert job["scoring"]["id"] == "rule-based"
        assert "nika_git_commit" in job
        assert job["official"] is True
        assert (result_dir / "RELEASE.lock.json").is_file()
        assert run_trials.called

        progress = written["progress"]
        assert progress["status"] == "running"
        assert progress["total_trials"] == 3
        assert progress["pending_trials"] == 3
        assert progress["result_dir"] == str(result_dir.resolve())
        assert progress["agent_type"] == "mock"
        assert progress["model"] == "mock-v1"

    def test_custom_timeout_forwarded(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        result_dir = tmp_path / "results"
        with (
            patch(
                "nika.workflows.benchmark.release.RELEASES_DIR", tmp_path / "releases"
            ),
            patch("nika.workflows.benchmark.run.preflight_release"),
            patch(
                "nika.workflows.benchmark.run._run_trials_batch", return_value=[]
            ) as run_trials,
            patch(
                "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
                tmp_path / "benchmark_runs",
            ),
        ):
            run_benchmark_from_release(
                release_ref="mini",
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                result_dir=str(result_dir),
                case_timeout=60,
                check_images=False,
                release=release,
            )
        job = json.loads((result_dir / JOB_FILENAME).read_text(encoding="utf-8"))
        assert job["case_timeout_sec"] == 60
        assert job["official"] is True
        assert run_trials.called
        assert run_trials.call_args.kwargs["case_timeout"] == 60
        assert "run_judge" not in run_trials.call_args.kwargs
