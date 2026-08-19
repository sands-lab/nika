"""CLI tests for task-mode ``nika agent run --problem``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from nika.cli.main import app

_RUNNER = CliRunner()


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
        with (
            patch(
                "nika.workflows.benchmark.task_label.resolve_default_inject_params",
                return_value={"host_name": "pc2", "intf_name": "eth0"},
            ) as resolve_mock,
            patch(
                "nika.workflows.benchmark.run.validate_inject_params"
            ) as validate_mock,
            patch(
                "nika.workflows.benchmark.run.run_single_case",
                return_value=("sid", tmp_path),
            ) as run_mock,
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
        resolve_mock.assert_called_once()
        assert resolve_mock.call_args.args[:3] == ("simple_bgp", "link_down", "")
        validate_mock.assert_called_once()
        run_mock.assert_called_once()
        kwargs = run_mock.call_args.kwargs
        assert kwargs["scenario"] == "simple_bgp"
        assert kwargs["problem"] == "link_down"
        assert kwargs["topo_size"] == ""
        assert kwargs["agent_type"] == "cli.claude"
        assert kwargs["inject_params"] == {"host_name": "pc2", "intf_name": "eth0"}
        assert kwargs["result_dir"] == str(tmp_path)

    def test_task_mode_sized_label_and_set_overrides(self, tmp_path: Path) -> None:
        with (
            patch(
                "nika.workflows.benchmark.task_label.resolve_default_inject_params",
                return_value={"host_name": "pc_0_0", "intf_name": "eth0"},
            ) as resolve_mock,
            patch("nika.workflows.benchmark.run.validate_inject_params"),
            patch(
                "nika.workflows.benchmark.run.run_single_case",
                return_value=("sid", tmp_path),
            ) as run_mock,
        ):
            result = _RUNNER.invoke(
                app,
                [
                    "agent",
                    "run",
                    "-a",
                    "cli.claude",
                    "--problem",
                    "dc_clos_bgp_s_link_down",
                    "--set",
                    "host_name=pc_0_1",
                ],
            )

        assert result.exit_code == 0, result.output
        assert resolve_mock.call_args.args[:3] == ("dc_clos", "link_down", "s")
        assert resolve_mock.call_args.kwargs["overrides"] == {"host_name": "pc_0_1"}
        assert run_mock.call_args.kwargs["scenario"] == "dc_clos"
        assert run_mock.call_args.kwargs["topo_size"] == "s"
        assert run_mock.call_args.kwargs["problem"] == "link_down"
        assert run_mock.call_args.kwargs["workload"] == "host"

    def test_session_mode_still_calls_start_agent(self) -> None:
        with patch(
            "nika.workflows.agent.run.start_agent",
            return_value=None,
        ) as start_mock:
            result = _RUNNER.invoke(
                app,
                ["agent", "run", "-a", "cli.claude", "--session_id", "sess-1"],
            )

        assert result.exit_code == 0, result.output
        start_mock.assert_called_once()
        assert start_mock.call_args.kwargs.get("session_id") == "sess-1" or (
            start_mock.call_args.args and "sess-1" in start_mock.call_args.args
        )
