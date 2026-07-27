"""Unit tests for frozen ``nika-bench`` releases and Dev/Test splits."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nika.workflows.benchmark.release import (
    JOB_FILENAME,
    ReleaseError,
    compute_benchmark_digest,
    freeze_release,
    load_release,
    parse_release_ref,
    preflight_release,
    resolve_cases,
    verify_dev_test_isolation,
)
from nika.workflows.benchmark.resume import benchmark_row_fingerprint
from nika.workflows.benchmark.run import run_benchmark_from_release
from tests.support.prerequisites import docker_available


def _mini_cases_yaml(path: Path) -> Path:
    payload = {
        "seed": 42,
        "cases": [
            {
                "scenario": "simple_bgp",
                "topo_size": None,
                "problem": "link_down",
                "inject": {"host_name": "pc1", "intf_name": "eth0"},
            }
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


class TestFrozenRelease010:
    def test_dev_and_test_counts(self) -> None:
        dev = load_release("0.1.0", split="dev")
        test = load_release("0.1.0", split="test")
        assert dev.case_count == 56
        assert test.case_count == 56
        assert int(dev.splits["dev"]["case_count"]) == 56
        assert int(test.splits["test"]["case_count"]) == 56
        assert "case_digests" not in dev.manifest
        assert dev.benchmark_digest == test.benchmark_digest
        assert dev.n_trials == 3
        assert test.n_trials == 3

    def test_heldout_isolation(self) -> None:
        dev = resolve_cases("0.1.0", split="dev")
        test = resolve_cases("0.1.0", split="test")
        verify_dev_test_isolation(dev_cases=dev, test_cases=test)
        assert {r["problem"] for r in dev} == {r["problem"] for r in test}
        assert len({r["problem"] for r in test}) == 56
        # Same problem, different instance.
        by_dev = {r["problem"]: r for r in dev}
        for row in test:
            assert benchmark_row_fingerprint(row) != benchmark_row_fingerprint(
                by_dev[row["problem"]]
            )

    def test_nika_alias_and_short_version(self) -> None:
        assert parse_release_ref("nika@0.1") == ("nika-bench", "0.1.0")
        a = load_release("nika@0.1", split="test")
        b = load_release("0.1.0", split="test")
        assert a.benchmark_digest == b.benchmark_digest
        assert a.cases == b.cases

    def test_resolve_cases_is_deterministic(self) -> None:
        a = resolve_cases("0.1.0", split="dev")
        b = resolve_cases("nika-bench@0.1.0", split="dev")
        assert a == b

    def test_digest_changes_when_split_hash_changes(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        original = release.benchmark_digest
        new_digest = compute_benchmark_digest(
            splits={
                "dev": {
                    "case_count": 1,
                    "cases_sha256": "0" * 64,
                }
            },
            defaults=release.defaults,
            scoring=release.scoring,
            tools=release.tools,
            resources=release.resources,
            images=release.images,
            scenario_problem_pin=release.scenario_problem_pin,
        )
        assert new_digest != original

    def test_sha256_ref_resolves(self) -> None:
        release = load_release("0.1.0", split="dev")
        by_digest = load_release(
            f"nika-bench@sha256:{release.benchmark_digest}", split="dev"
        )
        assert by_digest.version == release.version
        assert by_digest.benchmark_digest == release.benchmark_digest

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

    def test_preflight_missing_image(self, tmp_path: Path) -> None:
        source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
        release = freeze_release(
            version="mini",
            source_cases=source,
            out_dir=tmp_path / "releases" / "mini",
        )
        with patch(
            "nika.workflows.benchmark.release.image_exists",
            return_value=False,
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
        assert job["benchmark_digest"] == release.benchmark_digest
        assert job["case_count"] == 1
        assert job["n_trials"] == 3
        assert job["run_id"] == job["job_id"]
        assert job["scoring"]["id"] == "rule-based-v1"
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


@pytest.mark.skipif(not docker_available(), reason="Docker required for smoke")
class TestReleaseDockerSmoke:
    """Real Kathara smoke: one lightweight case from Dev and Test."""

    _LIGHT = frozenset(
        {
            "simple_bgp",
            "p4_bloom_filter",
            "p4_mpls",
            "p4_counter",
            "p4_int",
        }
    )

    def _pick_light_row(self, split: str) -> dict:
        rows = resolve_cases("0.1.0", split=split)
        for row in rows:
            if row["scenario"] in self._LIGHT:
                return row
        return rows[0]

    def _run_one(self, split: str) -> Path:
        row = self._pick_light_row(split)
        result_root = Path(tempfile.mkdtemp(prefix=f"nika-release-{split}-"))
        from nika.workflows.benchmark.release import (
            build_job_metadata,
            load_release,
            write_job_metadata,
        )
        from nika.workflows.benchmark.run import run_single_case

        release = load_release("0.1.0", split=split, verify_digest=True)
        job = build_job_metadata(
            release,
            agent_type="mock",
            model="mock-v1",
            case_timeout_sec=2400,
            official=True,
        )
        write_job_metadata(result_root, job)
        run_single_case(
            problem=row["problem"],
            scenario=row["scenario"],
            topo_size=row.get("topo_size") or "",
            agent_type="mock",
            llm_provider=None,
            model="mock-v1",
            max_steps=20,
            inject_params=row["inject"],
            result_dir=str(result_root),
            release_meta=job,
        )
        return result_root

    def test_dev_smoke(self) -> None:
        result_root = self._run_one("dev")
        job = json.loads((result_root / JOB_FILENAME).read_text(encoding="utf-8"))
        assert job["split"] == "dev"

    def test_test_smoke(self) -> None:
        result_root = self._run_one("test")
        job = json.loads((result_root / JOB_FILENAME).read_text(encoding="utf-8"))
        assert job["split"] == "test"
