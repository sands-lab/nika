"""Task layout, relative references, and portable benchmark materialization."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from nika.workflows.benchmark.load_config import (
    load_benchmark_input,
    normalize_benchmark_row,
)
from nika.workflows.benchmark.migrate import migrate_benchmark_yaml
from nika.workflows.benchmark.resume import benchmark_row_fingerprint
from nika.workflows.benchmark.trials import case_key_for_row

pytestmark = pytest.mark.contract


def _task():
    return {
        "scenario": "dc_clos",
        "topo_size": "s",
        "traffic": {"pattern": "incast", "incast_period_sec": 30},
        "schedule": {
            "agent_start": 0,
            "events": [
                {"traffic": "background", "at": 1},
                {
                    "problem": "link_down",
                    "inject": {"host_name": "client_1_1", "intf_name": "eth0"},
                    "at": 5,
                    "duration": 5,
                },
            ],
        },
    }


def _write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


@pytest.mark.parametrize("layout", ["single", "inline", "referenced"])
def test_task_layout_preserves_traffic_schedule_and_identity(tmp_path, layout):
    raw = _task()
    path = tmp_path / "benchmark.yaml"
    if layout == "single":
        _write(path, raw)
    elif layout == "inline":
        _write(path, {"cases": [raw]})
    else:
        _write(tmp_path / "tasks" / "uplink.yaml", raw)
        _write(path, {"cases": ["tasks/uplink.yaml"]})
    loaded = load_benchmark_input(path)
    expected = normalize_benchmark_row(raw)
    assert loaded == [expected]
    assert benchmark_row_fingerprint(loaded[0]) == benchmark_row_fingerprint(expected)
    assert case_key_for_row(loaded[0]) == case_key_for_row(expected)


def test_references_resolve_from_the_benchmark_and_keep_order(tmp_path, monkeypatch):
    first = _task()
    second = {"scenario": "dc_clos", "topo_size": "s", "problem": "healthy"}
    third = deepcopy(first)
    third["schedule"]["agent_start"] = 6
    _write(tmp_path / "tasks" / "one.yaml", first)
    _write(tmp_path / "tasks" / "two.yaml", second)
    _write(tmp_path / "tasks" / "three.yaml", third)
    path = _write(
        tmp_path / "benchmark.yaml",
        {"cases": ["tasks/one.yaml", "tasks/two.yaml", "tasks/three.yaml"]},
    )
    monkeypatch.chdir(tmp_path.parent)
    assert load_benchmark_input(path) == [
        normalize_benchmark_row(row) for row in (first, second, third)
    ]


@pytest.mark.parametrize("layout", ["single", "referenced"])
def test_migration_materializes_a_portable_matrix_without_changing_sources(
    tmp_path, layout
):
    task = _write(tmp_path / "tasks" / "uplink.yaml", _task())
    path = (
        task
        if layout == "single"
        else _write(
            tmp_path / "benchmark.yaml", {"seed": 42, "cases": ["tasks/uplink.yaml"]}
        )
    )
    original = {p: p.read_text() for p in (path, task)}
    output = tmp_path / "materialized.yaml"
    report = migrate_benchmark_yaml(
        input_path=path, output_path=output, report_path=tmp_path / "report.yaml"
    )
    assert report["case_count"] == 1 and report["unresolved_count"] == 0
    materialized = yaml.safe_load(output.read_text())
    assert materialized["cases"][0]["schedule"]["events"][1]["root_causes"]
    assert materialized["seed"] == (42 if layout == "referenced" else None)
    assert benchmark_row_fingerprint(
        load_benchmark_input(output)[0]
    ) == benchmark_row_fingerprint(load_benchmark_input(path)[0])
    assert all(p.read_text() == text for p, text in original.items())


@pytest.mark.parametrize("task", [{"cases": ["another.yaml"]}, {"schedule": {}}])
def test_references_require_a_complete_task(tmp_path, task):
    _write(tmp_path / "tasks" / "incomplete.yaml", task)
    path = _write(tmp_path / "benchmark.yaml", {"cases": ["tasks/incomplete.yaml"]})
    with pytest.raises(ValueError, match="one complete task"):
        load_benchmark_input(path)


@pytest.mark.parametrize("references_first", [True, False])
def test_mixed_inline_tasks_and_references_are_rejected(tmp_path, references_first):
    cases = ["tasks/uplink.yaml", _task()]
    path = _write(
        tmp_path / "benchmark.yaml",
        {"cases": cases if references_first else cases[::-1]},
    )
    with pytest.raises(ValueError, match="mixed entries are unsupported"):
        load_benchmark_input(path)


@pytest.mark.parametrize("field", ["scenario", "traffic", "schedule", "topo_size"])
def test_benchmark_cannot_supply_fields_to_referenced_tasks(tmp_path, field):
    path = _write(
        tmp_path / "benchmark.yaml", {field: _task()[field], "cases": ["task.yaml"]}
    )
    with pytest.raises(ValueError, match="Define task fields in each referenced task"):
        load_benchmark_input(path)


def test_missing_referenced_file_reports_its_path(tmp_path):
    path = _write(tmp_path / "benchmark.yaml", {"cases": ["tasks/missing.yaml"]})
    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        load_benchmark_input(path)


@pytest.mark.parametrize("entry", ["", None, 42])
def test_invalid_case_entries_fail_before_execution(tmp_path, entry):
    path = _write(tmp_path / "benchmark.yaml", {"cases": [entry]})
    with pytest.raises(ValueError):
        load_benchmark_input(path)
