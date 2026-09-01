"""CLI tests for task-mode ``nika agent run --problem``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nika.cli.main import app

_RUNNER = CliRunner()


@pytest.mark.unit
class TestAgentRunTaskMode:
    def test_help_mentions_problem(self) -> None:
        result = _RUNNER.invoke(app, ["agent", "run", "--help"])
        assert result.exit_code == 0
        assert "--problem" in result.output

    def test_problem_and_session_id_rejected(self) -> None:
        result = _RUNNER.invoke(
            app,
            [
                "agent",
                "run",
                "-a",
                "cli.claude",
                "--problem",
                "simple_bgp_link_down",
                "--session_id",
                "fake-session",
            ],
        )
        assert result.exit_code != 0
        combined = f"{result.output}\n{result.stderr or ''}"
        assert "--problem" in combined and "--session_id" in combined

    def test_set_without_problem_rejected(self) -> None:
        result = _RUNNER.invoke(
            app,
            ["agent", "run", "-a", "cli.claude", "--set", "host_name=pc1"],
        )
        assert result.exit_code != 0
        combined = f"{result.output}\n{result.stderr or ''}"
        assert "--set" in combined and "--problem" in combined

    def test_task_mode_routes_to_run_single_case(self, tmp_path: Path) -> None:
        captured: dict = {}

        def _capture_run_single_case(**kwargs):
            captured.update(kwargs)
            return ("sid", tmp_path)

        with (
            patch(
                "nika.workflows.benchmark.task_label.resolve_default_inject_params",
                return_value={"host_name": "pc2", "intf_name": "eth0"},
            ),
            patch("nika.workflows.benchmark.run.validate_inject_params"),
            patch(
                "nika.workflows.benchmark.run.run_single_case",
                side_effect=_capture_run_single_case,
            ),
        ):
            result = _RUNNER.invoke(
                app,
                [
                    "agent",
                    "run",
                    "-a",
                    "cli.claude",
                    "--problem",
                    "simple_bgp_link_down",
                    "--result_dir",
                    str(tmp_path),
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["scenario"] == "simple_bgp"
        assert captured["problem"] == "link_down"
        assert captured["topo_size"] == ""
        assert captured["agent_type"] == "cli.claude"
        assert captured["inject_params"] == {"host_name": "pc2", "intf_name": "eth0"}
        assert captured["result_dir"] == str(tmp_path)

    def test_task_mode_sized_label_and_set_overrides(self, tmp_path: Path) -> None:
        captured: dict = {}

        def _capture_run_single_case(**kwargs):
            captured.update(kwargs)
            return ("sid", tmp_path)

        with (
            patch(
                "nika.workflows.benchmark.task_label.resolve_default_inject_params",
                return_value={"host_name": "pc_0_0", "intf_name": "eth0"},
            ),
            patch("nika.workflows.benchmark.run.validate_inject_params"),
            patch(
                "nika.workflows.benchmark.run.run_single_case",
                side_effect=_capture_run_single_case,
            ),
        ):
            result = _RUNNER.invoke(
                app,
                [
                    "agent",
                    "run",
                    "-a",
                    "cli.claude",
                    "--problem",
                    "dc_clos_s_link_down",
                    "--set",
                    "host_name=pc_0_1",
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["scenario"] == "dc_clos"
        assert captured["topo_size"] == "s"
        assert captured["problem"] == "link_down"
        assert "workload" not in captured

    def test_session_mode_still_succeeds(self) -> None:
        with patch("nika.workflows.agent.run.start_agent", return_value=None):
            result = _RUNNER.invoke(
                app,
                ["agent", "run", "-a", "cli.claude", "--session_id", "sess-1"],
            )

        assert result.exit_code == 0, result.output
