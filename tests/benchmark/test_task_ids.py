"""Public task ids, catalog lookup, and --task-id filtering."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nika.cli.main import app
from nika.run_config.loader import ENV_RUN_CONFIG, reset_run_config
from nika.workflows.benchmark.release import load_release
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import (
    catalog_entries,
    describe_task_payload,
    expand_trials,
    index_rows_by_task_id,
    parse_task_selector,
    resolve_catalog_row,
    select_trials,
    task_id_for_row,
    trial_dirname,
)
from tests.benchmark.trial_helpers import ROW_A, ROW_B, mini_cases_yaml

pytestmark = pytest.mark.unit

_RUNNER = CliRunner()
TASK_A = "dc_clos__link_down__s__host_name-client_0__intf_name-eth0"
TASK_B = "dc_clos__link_flap__s__host_name-client_0__intf_name-eth0"


@pytest.fixture(autouse=True)
def _isolate_run_config(monkeypatch: pytest.MonkeyPatch):
    """CLI invokes persist effective config; do not leak release into later tests."""
    monkeypatch.delenv(ENV_RUN_CONFIG, raising=False)
    reset_run_config()
    yield
    reset_run_config()


class TestTaskSelectors:
    def test_parse_case_and_trial_ids(self) -> None:
        assert parse_task_selector(TASK_A) == (TASK_A, None)
        assert parse_task_selector(f"{TASK_A}__t02") == (TASK_A, 2)
        assert parse_task_selector(f"  {TASK_A}__t12  ") == (TASK_A, 12)

    def test_parse_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            parse_task_selector("  ")

    def test_task_id_matches_case_key(self) -> None:
        assert task_id_for_row(ROW_A) == TASK_A
        assert task_id_for_row(ROW_B) == TASK_B

    def test_duplicate_task_ids_raise(self) -> None:
        with pytest.raises(ValueError, match="Duplicate task_id"):
            index_rows_by_task_id([ROW_A, dict(ROW_A)])

    def test_resolve_strips_trial_suffix(self) -> None:
        row = resolve_catalog_row([ROW_A, ROW_B], f"{TASK_B}__t03")
        assert row["problem"] == "link_flap"

    def test_resolve_unknown_id(self) -> None:
        with pytest.raises(ValueError, match="Unknown task id"):
            resolve_catalog_row([ROW_A], "missing-task")

    def test_describe_payload_includes_inject(self) -> None:
        payload = describe_task_payload(ROW_A)
        assert payload["task_id"] == TASK_A
        assert payload["inject"] == {"host_name": "client_0", "intf_name": "eth0"}
        assert "root_causes" not in payload


class TestSelectTrials:
    def test_filter_by_task_id_keeps_all_trials(self) -> None:
        trials = expand_trials([ROW_A, ROW_B], n_trials=3)
        selected = select_trials(trials, [TASK_B])
        assert [item.trial_id for item in selected] == [
            trial_dirname(TASK_B, index) for index in (1, 2, 3)
        ]

    def test_filter_by_trial_dirname(self) -> None:
        trials = expand_trials([ROW_A, ROW_B], n_trials=3)
        selected = select_trials(trials, [f"{TASK_A}__t02"])
        assert [item.trial_id for item in selected] == [trial_dirname(TASK_A, 2)]

    def test_filter_unknown_id(self) -> None:
        trials = expand_trials([ROW_A], n_trials=1)
        with pytest.raises(ValueError, match="Unknown task id"):
            select_trials(trials, ["no-such-task"])

    def test_empty_selectors_keep_all(self) -> None:
        trials = expand_trials([ROW_A], n_trials=2)
        assert select_trials(trials, []) == trials


class TestReleaseTaskIdGate:
    def test_unknown_task_id_fails_before_preflight(self, tmp_path: Path) -> None:
        release = load_release("0.2.0", split="test")
        result_dir = tmp_path / "run"
        with (
            patch("nika.workflows.benchmark.run.preflight_release") as preflight,
            patch("nika.workflows.benchmark.run.run_benchmark_trials") as runner,
        ):
            with pytest.raises(ValueError, match="Unknown task id"):
                run_benchmark_from_release(
                    "0.2.0",
                    agent_type="mock",
                    llm_provider=None,
                    model="mock-v1",
                    max_steps=None,
                    split="test",
                    result_dir=str(result_dir),
                    check_images=False,
                    release=release,
                    task_ids=["missing-task"],
                )
        preflight.assert_not_called()
        runner.assert_not_called()
        assert not (result_dir / "run.json").exists()

    def test_scoped_run_records_planned_trial_count(self, tmp_path: Path) -> None:
        release = load_release("0.2.0", split="test")
        task_id = task_id_for_row(release.cases[0])
        result_dir = tmp_path / "run"
        with (
            patch("nika.workflows.benchmark.run.preflight_release"),
            patch("nika.workflows.benchmark.run.run_benchmark_trials") as runner,
        ):
            run_benchmark_from_release(
                "0.2.0",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                split="test",
                result_dir=str(result_dir),
                check_images=False,
                release=release,
                task_ids=[f"{task_id}__t02"],
            )
        runner.assert_called_once()
        job = json.loads((result_dir / "run.json").read_text(encoding="utf-8"))
        assert job["task_ids"] == [f"{task_id}__t02"]
        assert job["planned_trial_count"] == 1
        assert job["case_count"] == release.case_count


class TestPublishedReleaseCatalog:
    def test_0_2_0_test_split_has_unique_task_ids(self) -> None:
        release = load_release("0.2.0", split="test")
        entries = catalog_entries(release.cases)
        ids = [entry["task_id"] for entry in entries]
        assert len(ids) == 85
        assert len(set(ids)) == 85


class TestBenchmarkCatalogCli:
    def test_list_and_describe_config(self, tmp_path: Path) -> None:
        path = mini_cases_yaml(tmp_path / "cases.yaml")
        listed = _RUNNER.invoke(app, ["benchmark", "list", "--config", str(path)])
        assert listed.exit_code == 0, listed.output
        header = listed.output.splitlines()[0]
        assert header.startswith("SCENARIO")
        assert header.rstrip().endswith("TASK_ID")
        assert TASK_A in listed.output
        assert TASK_B in listed.output
        assert "link_down" in listed.output

        described = _RUNNER.invoke(
            app,
            ["benchmark", "describe", TASK_A, "--config", str(path)],
        )
        assert described.exit_code == 0, described.output
        assert f"task_id: {TASK_A}" in described.output
        assert "host_name: client_0" in described.output
        assert "intf_name: eth0" in described.output

        from_trial = _RUNNER.invoke(
            app,
            ["benchmark", "describe", f"{TASK_A}__t01", "--config", str(path)],
        )
        assert from_trial.exit_code == 0, from_trial.output
        assert f"task_id: {TASK_A}" in from_trial.output

    def test_list_json(self, tmp_path: Path) -> None:
        path = mini_cases_yaml(tmp_path / "cases.yaml")
        result = _RUNNER.invoke(
            app, ["benchmark", "list", "--config", str(path), "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert [row["task_id"] for row in payload] == [TASK_A, TASK_B]

    def test_describe_unknown_id(self, tmp_path: Path) -> None:
        path = mini_cases_yaml(tmp_path / "cases.yaml")
        result = _RUNNER.invoke(
            app, ["benchmark", "describe", "missing-task", "--config", str(path)]
        )
        assert result.exit_code != 0
        text = f"{result.output}\n{result.stderr or ''}"
        assert "Unknown task id" in text

    def test_run_forwards_task_id(self, tmp_path: Path) -> None:
        path = mini_cases_yaml(tmp_path / "cases.yaml")
        with patch(
            "nika.cli.commands.benchmark.run_benchmark_from_yaml"
        ) as run_from_yaml:
            result = _RUNNER.invoke(
                app,
                [
                    "benchmark",
                    "run",
                    "--config",
                    str(path),
                    "--task-id",
                    TASK_A,
                    "--task-id",
                    f"{TASK_B}__t01",
                    "-m",
                    "mock-v1",
                ],
            )
        assert result.exit_code == 0, result.output
        assert run_from_yaml.call_args.kwargs["task_ids"] == [
            TASK_A,
            f"{TASK_B}__t01",
        ]

    def test_run_rejects_task_id_in_single_case_mode(self) -> None:
        result = _RUNNER.invoke(
            app,
            [
                "benchmark",
                "run",
                "dc_clos",
                "--problem",
                "link_down",
                "-s",
                "s",
                "--set",
                "host_name=client_0",
                "--set",
                "intf_name=eth0",
                "--task-id",
                TASK_A,
                "-m",
                "mock-v1",
            ],
        )
        assert result.exit_code != 0
        text = f"{result.output}\n{result.stderr or ''}"
        assert "--task-id" in text

    def test_run_forwards_task_id_to_release(self) -> None:
        with patch(
            "nika.cli.commands.benchmark.run_benchmark_from_release"
        ) as run_from_release:
            result = _RUNNER.invoke(
                app,
                [
                    "benchmark",
                    "run",
                    "--release",
                    "0.2.0",
                    "--split",
                    "test",
                    "--task-id",
                    TASK_A,
                    "--task-id",
                    f"{TASK_A}__t02",
                    "-m",
                    "mock-v1",
                ],
            )
        assert result.exit_code == 0, result.output
        assert run_from_release.call_args.kwargs["task_ids"] == [
            TASK_A,
            f"{TASK_A}__t02",
        ]
