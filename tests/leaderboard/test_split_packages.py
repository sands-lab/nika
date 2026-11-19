"""Release runs, packaging, and cross-split comparisons without external services."""

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from nika.cli.main import app
from nika.evaluator.scoring import score_detection, score_rca
from nika.workflows.benchmark.network_identity import dataset_row
from nika.workflows.benchmark.load_config import normalize_benchmark_row
from nika.workflows.benchmark.release import (
    RELEASE_DEFAULTS,
    RELEASE_RESOURCES,
    RELEASE_TOOLS,
    SCORING,
    load_release_from_dir,
    write_release_manifest,
)
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import task_id_for_row, trial_dir
from nika.workflows.leaderboard.comparison import (
    configuration_mismatches,
    summarize_split_packages,
)
from nika.workflows.leaderboard.pack import pack_leaderboard_submission
from nika.workflows.leaderboard.validate import validate_leaderboard_submission
from tests.leaderboard.test_pack_validate import _FULL_METADATA, mini_release_env  # noqa: F401


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def _row(size, problem="link_down", slice_name="core", profile="frr"):
    faults = [] if problem == "healthy" else problem.split("+")
    return dataset_row(
        {
            "scenario": "dc_clos",
            "topo_size": size,
            "device_profile": profile,
            "backend": "containerlab" if profile == "sonic" else "kathara",
            "problem": problem,
            "inject": (
                {}
                if not faults
                else {"host_name": "spine_1", "intf_name": "eth0"}
                if len(faults) == 1
                else {
                    fault: {"host_name": "spine_1", "intf_name": "eth0"}
                    for fault in faults
                }
            ),
            "root_causes": [
                {"resource": {"kind": "node", "node": "spine_1"}, "fault_type": fault}
                for fault in faults
            ],
            **({"problems": faults} if len(faults) > 1 else {}),
        },
        slice_name=slice_name,
    )


def _mock_trial(trial, **kwargs):
    path = trial_dir(Path(kwargs["result_dir"]), trial.case_key, trial.trial_index)
    gt = {
        "is_anomaly": trial.row["problem"] != "healthy",
        "root_causes": trial.row["root_causes"],
    }
    # Dev core: 1/2; Test core: 1; multi-fault: 0; optional slices: 1.
    correct = trial.row.get("slice") != "multi_fault" and not (
        trial.row.get("slice") == "core"
        and trial.row["topo_size"] == "s"
        and trial.trial_index == 2
    )
    answer = copy.deepcopy(gt) if correct else {"is_anomaly": False, "root_causes": []}
    meta = kwargs["release_meta"]
    _write(
        path / "run.json",
        {
            "session_id": trial.trial_id,
            "status": "finished",
            "outcome": "success",
            "score_status": "scored",
            "trial_id": trial.trial_id,
            "trial_index": trial.trial_index,
            "case_key": trial.case_key,
            "scenario_name": trial.row["scenario"],
            "problem_names": trial.row["problem"].split("+"),
            "agent_type": "mock",
            "model": "mock-v1",
            "benchmark_run_id": meta["run_id"],
            "benchmark_version": meta["version"],
            "benchmark_split": meta["split"],
        },
    )
    _write(path / "ground_truth.json", gt)
    _write(path / "submission.json", answer)
    _write(
        path / "eval_metrics.json",
        {
            **score_rca(answer, gt),
            "detection_score": score_detection(answer, gt),
            "in_tokens": 20,
            "out_tokens": 8,
            "steps": 1,
            "tool_calls": 0,
            "tool_errors": 0,
        },
    )
    _write(
        path / "messages.jsonl",
        {
            "role": "assistant",
            "content": "Mock diagnosis for local pipeline verification.",
        },
    )
    _write(path / "nika.jsonl", {"event": "mock_trial_finished"})


@pytest.mark.e2e
def test_release_run_pack_split_summary(tmp_path, monkeypatch):
    """Run two scratch 0.3.0 splits; the mock worker uses the real scorer."""
    root = tmp_path / "releases/0.3.0"
    root.mkdir(parents=True)
    core = _row("s")
    twin = _row("s", slice_name="vendor_twin", profile="sonic")
    twin.update(
        base_task_id=task_id_for_row(normalize_benchmark_row(core)), vendor_tier="C"
    )
    third = _row("l", slice_name="third_size")
    datasets = {
        "dev": [core, _row("s", "healthy", "healthy"), twin, third],
        "test": [
            _row("m"),
            _row("m", "link_flap"),
            _row("m", "link_down+link_flap", "multi_fault"),
        ],
    }
    for split, rows in datasets.items():
        (root / f"{split}.yaml").write_text(
            yaml.safe_dump({"cases": rows}, sort_keys=False)
        )
    write_release_manifest(
        root,
        version="0.3.0",
        splits={
            s: {"cases_file": f"{s}.yaml", "case_count": len(rows)}
            for s, rows in datasets.items()
        },
        defaults={**RELEASE_DEFAULTS, "n_trials": 2},
        scoring=SCORING,
        tools=RELEASE_TOOLS,
        resources=RELEASE_RESOURCES,
        images={"required": []},
    )
    monkeypatch.setattr("nika.workflows.benchmark.release.RELEASES_DIR", root.parent)
    monkeypatch.setattr(
        "nika.workflows.benchmark.release.read_git_commit", lambda: (None, False)
    )

    packages = []
    for split in ("dev", "test", "dev+test"):
        release = load_release_from_dir(root, split=split)
        result = tmp_path / split
        with (
            patch("nika.workflows.benchmark.run.preflight_release"),
            patch(
                "nika.workflows.benchmark.run._run_trial_with_timeout",
                side_effect=_mock_trial,
            ),
        ):
            run_benchmark_from_release(
                release_ref="0.3.0",
                split=split,
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=10,
                result_dir=str(result),
                case_timeout=0,
                check_images=False,
                release=release,
                include_slices=True,
                yes=True,
            )
        staging = result / "submission"
        staging.mkdir()
        metadata = copy.deepcopy(_FULL_METADATA)
        metadata["agent"]["extra"] = {"comparison_id": "local-030"}
        (staging / "metadata.yaml").write_text(yaml.safe_dump(metadata))
        (staging / "README.md").write_text(
            "# Local mock\n\nLocal pipeline verification.\n"
        )
        outcome = CliRunner().invoke(
            app,
            [
                "leaderboard",
                "submit",
                "--result_dir",
                str(result),
                "--submission",
                str(staging),
                "--dry-run",
                "--release-source",
                "local",
            ],
        )
        assert outcome.exit_code == 0, outcome.output
        print(outcome.output)
        package = next(p for p in result.glob("*_mock_mock_v1") if p.is_dir())
        report = validate_leaderboard_submission(
            package,
            release_source="local",
            run_config_reference=packages[0] if packages else None,
        )
        assert report.ok, report.errors
        print(report.render_text())
        packages.append(package)
    summary = summarize_split_packages(packages[:2], release_source="local")
    combined_summary = summarize_split_packages([packages[2]], release_source="local")
    assert combined_summary == summary
    assert summary["main_scores"] == {"dev": 0.75, "test": 2 / 3, "dev_test": 0.7}
    assert summary["split_metrics"]["dev"]["healthy_false_positive_rate"] == 0
    assert summary["split_metrics"]["test"]["multi_fault_mean_rca_f1"] == 0
    assert summary["combined_metrics"]["vendor_deltas"]["C"]["sonic"]["delta"] == 0.5
    assert summary["combined_metrics"]["vendor_deltas"]["C"]["sonic"]["ci95"] is None
    assert (
        summary["combined_metrics"]["size_slices"]["generator_families"]["dc_clos"][
            "l"
        ]["mean_rca_f1"]
        == 1
    )
    # A requested but missing score contributes zero, as in package validation.
    result_paths = sorted((packages[0] / "results/trials").glob("*/result.json"))
    result_path = next(
        path
        for path in result_paths
        if json.loads(path.read_text())["dataset"].get("slice") == "healthy"
    )
    trial = json.loads(result_path.read_text())
    trial["metrics"]["detection_score"] = None
    _write(result_path, trial)
    revised = summarize_split_packages(packages[:2], release_source="local")
    assert revised["split_metrics"]["dev"]["mean_detection_score"] == pytest.approx(
        summary["split_metrics"]["dev"]["mean_detection_score"] - 0.25
    )
    _write(tmp_path / "split-summary.json", summary)
    _write(tmp_path / "packages.json", [str(p) for p in packages])
    print(json.dumps(summary["main_scores"], indent=2))


@pytest.mark.contract
def test_reference_rejects_mismatched_config(mini_release_env):  # noqa: F811
    _, result = mini_release_env
    first = pack_leaderboard_submission(
        result,
        metadata=_FULL_METADATA,
        readme_text="# Reference",
        out_dir=result / "reference" / "20261115_mock_mock_v1",
    ).scores_dir
    # A helper's declared skills differ, while the run results remain identical.
    metadata = copy.deepcopy(_FULL_METADATA)
    metadata["agent"]["skills"] = ["different-skills"]
    second = pack_leaderboard_submission(
        result,
        metadata=metadata,
        readme_text="# Helper",
        out_dir=result / "helper" / "20261115_mock_mock_v1",
    ).scores_dir
    assert configuration_mismatches(second, first)
    report = validate_leaderboard_submission(second, run_config_reference=first)
    assert not report.ok
    assert any(issue.code == "ERROR_RUN_CONFIG_MISMATCH" for issue in report.issues)


@pytest.mark.contract
def test_slice_metadata_must_match_release(mini_release_env):  # noqa: F811
    _, result = mini_release_env
    package = pack_leaderboard_submission(
        result, metadata=_FULL_METADATA, readme_text="# Entry"
    ).scores_dir
    trial_path = next((package / "results/trials").glob("*/result.json"))
    trial = json.loads(trial_path.read_text())
    trial["dataset"] = {"slice": "third_size"}
    trial_path.write_text(json.dumps(trial))
    report = validate_leaderboard_submission(package)
    assert not report.ok
    assert any("dataset metadata differs" in error for error in report.errors)
