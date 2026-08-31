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
    is_deprecated_release,
    load_release,
    parse_release_ref,
    preflight_release,
    resolve_cases,
    verify_dev_test_isolation,
)
from nika.workflows.benchmark.run import run_benchmark_from_release


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


class TestDeprecatedRelease010:
    def test_is_marked_deprecated(self) -> None:
        assert is_deprecated_release("0.1.0")
        assert is_deprecated_release(" 0.1.0 ")
        assert not is_deprecated_release("mini")

    def test_load_raises_deprecated(self) -> None:
        with pytest.raises(ReleaseError, match="deprecated"):
            load_release("0.1.0", split="dev")
        with pytest.raises(ReleaseError, match="deprecated"):
            resolve_cases("0.1.0", split="test")

    def test_nika_alias_still_parses_but_load_rejects(self) -> None:
        assert parse_release_ref("nika@0.1") == ("nika-bench", "0.1.0")
        with pytest.raises(ReleaseError, match="deprecated"):
            load_release("nika@0.1", split="test")

    def test_sha256_ref_rejected(self) -> None:
        with pytest.raises(ReleaseError, match="Digest-based"):
            load_release(
                "nika-bench@sha256:226d3209e3c3c46aade8c37ecd989642ae73692f0d9f149995954553e41474d1",
                split="dev",
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

        with (
            patch(
                "nika.workflows.benchmark.run.preflight_release",
                return_value=None,
            ),
            patch(
                "nika.workflows.benchmark.run.run_benchmark_trials",
                return_value=None,
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

        progress_path = runs_dir / f"{job['run_id']}.json"
        assert progress_path.is_file()
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
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
            patch("nika.workflows.benchmark.run.preflight_release"),
            patch("nika.workflows.benchmark.run.run_benchmark_trials") as run_trials,
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

    def test_run_deprecated_release_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ReleaseError, match="deprecated"):
            run_benchmark_from_release(
                release_ref="0.1.0",
                split="test",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                result_dir=str(tmp_path / "results"),
                check_images=False,
            )
