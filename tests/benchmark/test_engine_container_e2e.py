"""A release that pins a benchmark engine runs on that engine's container.

Runs selected release tasks with the oracle agent. By default the engine is
built from committed ``HEAD``; CI can supply a prebuilt image or select the
release's pinned engine and compare each task with an in-process run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from nika.benchmark_engine.container import RUN_LABEL, _git_commit, image_id
from nika.config import SESSIONS_DIR
from nika.problems.rca.materialize import assert_root_causes_match
from nika.runtime.factory import runtime_for_session
from nika.workflows.benchmark.release import load_release
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import is_valid_trial, task_id_for_row
from tests.benchmark.trial_helpers import pinned_engine_release
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]

TASK = "dc_clos__receiver_resource_c__ha2be5b357067"


def _engine_cases() -> list:
    selected = os.environ.get("NIKA_CI_ENGINE_TASK_ID", TASK).strip()
    cases = []
    release_ref = os.environ.get("NIKA_CI_ENGINE_RELEASE", "0.2.0")
    for split in os.environ.get("NIKA_CI_ENGINE_SPLIT", "test").split(","):
        split = split.strip()
        release = load_release(release_ref, split=split)
        matching = [
            row
            for row in release.cases
            if selected == "all" or task_id_for_row(row) == selected
        ]
        if not matching:
            raise ValueError(f"No engine task {selected!r} in {split!r}")
        cases.extend(
            pytest.param(split, row, id=f"{split}-{task_id_for_row(row)}")
            for row in matching
        )
    shard_index = int(os.environ.get("NIKA_CI_ENGINE_SHARD_INDEX", "0"))
    shard_count = int(os.environ.get("NIKA_CI_ENGINE_SHARD_COUNT", "1"))
    if not 0 <= shard_index < shard_count:
        raise ValueError("Engine shard index must be in [0, shard count)")
    return cases[shard_index::shard_count]


def _run_task(result_dir: Path, release_ref: str, split: str, task: dict) -> Path:
    try:
        run_benchmark_from_release(
            release_ref=release_ref,
            split=split,
            agent_type="oracle",
            llm_provider=None,
            model=None,
            max_steps=None,
            result_dir=str(result_dir),
            task_ids=[f"{task_id_for_row(task)}__t01"],
            yes=True,
        )
    finally:
        for run_path in (result_dir / "trials").glob("*/run.json"):
            run_meta = json.loads(run_path.read_text())
            assert not (SESSIONS_DIR / f"{run_meta['session_id']}.json").exists()
            if run_meta.get("lab_name"):
                # The frontend needs no host Containerlab installation to run a
                # pinned engine. Inspect its Docker label directly instead.
                if run_meta.get("backend") == "containerlab":
                    remaining = subprocess.run(
                        [
                            "docker",
                            "ps",
                            "-aq",
                            "--filter",
                            f"label=containerlab={run_meta['lab_name']}",
                        ],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    assert not remaining.stdout.strip(), run_meta["lab_name"]
                else:
                    runtime = runtime_for_session(run_meta)
                    assert not runtime.exists(), run_meta["lab_name"]
                    assert not runtime.has_leftover_resources(), run_meta["lab_name"]
                helpers = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        f"label=nika.lab_name={run_meta['lab_name']}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                assert not helpers.stdout.strip(), run_meta["lab_name"]
    trials = list((result_dir / "trials").iterdir())
    assert len(trials) == 1
    return trials[0]


def _assert_trial(trial: Path, task: dict) -> dict:
    assert is_valid_trial(trial)
    assert json.loads((trial / "run.json").read_text())["outcome"] == "success"
    metrics = json.loads((trial / "eval_metrics.json").read_text())
    assert metrics["rca_f1"] == 1.0
    ground_truth = json.loads((trial / "ground_truth.json").read_text())
    anomalous = task["problem"] != "healthy"
    assert ground_truth["is_anomaly"] is anomalous
    assert_root_causes_match(ground_truth, task.get("root_causes") or [])
    events = [
        json.loads(line) for line in (trial / "nika.jsonl").read_text().splitlines()
    ]
    presence = [
        event["data"]
        for event in events
        if event.get("event") == "fault_presence_recheck"
    ]
    assert {check["phase"] for check in presence} >= {
        "post_inject",
        "during_agent",
        "before_cleanup",
    }
    assert all(
        check["result"] == ("present" if anomalous else "not_applicable")
        for check in presence
    ), presence
    tools = (trial / "tool_calls.jsonl").read_text().splitlines()
    assert tools and all(not json.loads(t)["is_error"] for t in tools)
    return metrics


@pytest.mark.parametrize("split,task", _engine_cases())
def test_pinned_release_runs_on_engine_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, split: str, task: dict
) -> None:
    release_ref = os.environ.get("NIKA_CI_ENGINE_RELEASE", "0.2.0")
    pinned = os.environ.get("NIKA_CI_ENGINE_PINNED") == "1"
    with pinned_engine_release(
        tmp_path, monkeypatch, release_ref=release_ref, pinned=pinned
    ) as (tag, image):
        errors = []
        results = {}
        metrics = None
        for mode in ("engine", "in_process"):
            if mode == "in_process" and os.environ.get("NIKA_CI_ENGINE_COMPARE") != "1":
                continue
            result_dir = tmp_path / ("run" if mode == "engine" else "in_process")
            result = {"result_dir": str(result_dir), "accepted": False}
            results[mode] = result
            try:
                if mode == "in_process":
                    manifest_path = load_release(release_ref).root / "RELEASE.yaml"
                    manifest = yaml.safe_load(manifest_path.read_text())
                    manifest.pop("engine", None)
                    manifest_path.write_text(yaml.safe_dump(manifest))
                trial = _run_task(result_dir, release_ref, split, task)
                actual_metrics = _assert_trial(trial, task)
                if mode == "engine":
                    metrics = actual_metrics
                    provenance = json.loads((result_dir / "run.json").read_text())[
                        "provenance"
                    ]
                    assert provenance["engine"]["tag"] == tag
                    assert provenance["engine"]["image"] == image_id(image)
                    assert provenance["engine"]["commit"] == _git_commit(tag)
                    assert provenance["engine"]["dirty"] is False
                    assert provenance["engine"]["api"] == 1
                    engine_log = (result_dir / "benchmark_engine.log").read_text()
                    assert "Started network environment" in engine_log
                    assert "Created new" in engine_log
                elif metrics is not None:
                    for name in ("rca_f1", "localization_f1", "detection_score"):
                        assert actual_metrics[name] == metrics[name], name
                result["accepted"] = True
            except Exception as exc:
                exc.add_note(f"{mode} acceptance: {result_dir}")
                errors.append(exc)
                result["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                result["trials"] = [
                    {
                        "path": str(path.parent),
                        "outcome": json.loads(path.read_text()).get("outcome"),
                    }
                    for path in (result_dir / "trials").glob("*/run.json")
                ]
                (tmp_path / "comparison.json").write_text(json.dumps(results, indent=2))

        # This run's engine container, including when task acceptance failed.
        try:
            remaining = subprocess.run(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    f"label={RUN_LABEL}={load_release(release_ref).version}",
                    "--filter",
                    f"volume={(tmp_path / 'run').resolve()}",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert not remaining.stdout.strip(), (
                f"Engine containers remain: {remaining.stdout.strip()}"
            )
        except Exception as exc:
            exc.add_note("Engine container cleanup check")
            errors.append(exc)
            results["engine"]["accepted"] = False
            results["engine"]["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        (tmp_path / "comparison.json").write_text(json.dumps(results, indent=2))
        if errors:
            raise ExceptionGroup("Engine task acceptance failed", errors)


def test_legacy_release_keeps_its_published_topology(tmp_path: Path) -> None:
    release_ref = "0.1.0"
    release = load_release(release_ref, split="dev")
    task = next(
        row
        for row in release.cases
        if row["scenario"] == "dc_clos"
        and row["problem"] == "link_down"
        and row.get("topo_size") == "s"
    )
    trial = _run_task(tmp_path / "legacy-run", release_ref, "dev", task)
    _assert_trial(trial, task)
    meta = json.loads((trial / "run.json").read_text())
    nodes = set(meta["metadata"]["machine_identities"])
    assert {"client_0", "webserver0_pod0", "super_spine_router_0"} <= nodes
