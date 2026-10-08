"""User-facing case discovery, keyboard navigation, and portable YAML exports."""

import json
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from typer.testing import CliRunner

from nika.cli.main import app
from nika.cli.prompts import choose
from nika.config import BENCHMARK_DIR
from nika.workflows.benchmark.load_config import load_benchmark_input
from nika.workflows.benchmark.trials import task_id_for_row

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ("SECOND\r", "b"),
        ("\x1b[B\r", "b"),
        ("\x1b", None),
    ],
)
def test_searchable_choices(keys: str, expected: str | None) -> None:
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys)
            assert (
                choose("Choose", [("a", "First preset"), ("b", "Second preset")])
                == expected
            )


def test_choice_cancel() -> None:
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text("\x03")
            with pytest.raises(KeyboardInterrupt):
                choose("Choose", [("a", "First preset")])


def test_case_export_preserves_task_identity_and_ground_truth(tmp_path: Path) -> None:
    runner = CliRunner()
    catalog = BENCHMARK_DIR / "working" / "pool" / "dc_clos" / "host_missing_ip.yaml"
    listing = runner.invoke(
        app,
        [
            "case",
            "browse",
            "--catalog",
            str(catalog),
            "--env",
            "dc_clos",
            "--failure",
            "host_missing_ip",
            "--json",
        ],
    )
    assert listing.exit_code == 0, listing.output
    entries = json.loads(listing.output)
    assert [row["topo_size"] for row in entries] == ["s", "m", "l"]
    assert all(row["e2e_verification"] == "not_recorded" for row in entries)
    row = entries[0]
    output = tmp_path / "case with spaces.yaml"
    args = [
        "case",
        "browse",
        "--catalog",
        str(catalog),
        "--task-id",
        row["task_id"],
        "--output",
        str(output),
    ]
    exported = runner.invoke(app, args)
    assert exported.exit_code == 0, exported.output
    reloaded = load_benchmark_input(output)
    assert len(reloaded) == 1
    assert task_id_for_row(reloaded[0]) == row["task_id"]
    assert reloaded[0]["inject"] == row["inject"]
    assert reloaded[0]["root_causes"] == row["root_causes"]
    original = output.read_bytes()
    duplicate = runner.invoke(app, args)
    assert duplicate.exit_code != 0
    assert output.read_bytes() == original


def test_noninteractive_browse_requires_selection() -> None:
    catalog = BENCHMARK_DIR / "working" / "pool" / "dc_clos" / "host_missing_ip.yaml"
    result = CliRunner().invoke(app, ["case", "browse", "--catalog", str(catalog)])
    assert result.exit_code != 0
    assert "--json" in result.output
    assert "--task-id" in result.output


def test_incompatible_case_is_not_offered(tmp_path: Path) -> None:
    catalog = tmp_path / "cases.yaml"
    catalog.write_text(
        "cases:\n- scenario: dc_clos\n  topo_size: s\n  problem: k8s_coredns_isolated\n  inject:\n    host_name: client_0\n"
    )
    result = CliRunner().invoke(
        app, ["case", "browse", "--catalog", str(catalog), "--json"]
    )
    assert result.exit_code != 0
    assert "No matching" in result.output


@pytest.mark.parametrize("command", ["browse", "run"])
def test_malformed_catalog_reports_cli_error(tmp_path: Path, command: str) -> None:
    catalog = tmp_path / "broken.yaml"
    catalog.write_text("cases: [\n", encoding="utf-8")
    args = ["case", command]
    if command == "run":
        args.append("invalid-task")
    result = CliRunner().invoke(app, [*args, "--catalog", str(catalog)])
    assert result.exit_code == 2, result.output
    assert "Invalid value" in result.output
