from __future__ import annotations
import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from nika.cli.main import app


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in (here, *here.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError(f"Could not locate repository root from {here}")


_REPO_ROOT = _repo_root()
_RUNNER = CliRunner()
CLI_COMMAND_MODULES = [
    "nika.cli.commands.agent",
    "nika.cli.commands.benchmark",
    "nika.cli.commands.config_cmd",
    "nika.cli.commands.env",
    "nika.cli.commands.evaluation",
    "nika.cli.commands.exec",
    "nika.cli.commands.failure",
    "nika.cli.commands.remote",
    "nika.cli.commands.session",
    "nika.cli.commands.traffic",
]
CLI_HANDLER_WORKFLOWS = [
    "nika.workflows.agent.run",
    "nika.workflows.benchmark.run",
    "nika.workflows.benchmark.task_label",
    "nika.workflows.env.start",
    "nika.workflows.eval.clean",
    "nika.workflows.eval.session",
    "nika.workflows.eval.summary",
    "nika.workflows.exec.command",
    "nika.workflows.failure.inject",
    "nika.workflows.session.close",
    "nika.workflows.session.containers",
    "nika.workflows.session.inspect",
    "nika.workflows.session.list",
]
CLI_HELP_ARGS = [
    ["--help"],
    ["agent", "--help"],
    ["agent", "list", "--help"],
    ["agent", "run", "--help"],
    ["benchmark", "--help"],
    ["benchmark", "run", "--help"],
    ["config", "--help"],
    ["config", "show", "--help"],
    ["config", "migrate", "--help"],
    ["env", "--help"],
    ["env", "list", "--help"],
    ["env", "run", "--help"],
    ["env", "ps", "--help"],
    ["eval", "--help"],
    ["eval", "metrics", "--help"],
    ["eval", "judge", "--help"],
    ["eval", "summary", "--help"],
    ["eval", "clean", "--help"],
    ["exec", "--help"],
    ["failure", "--help"],
    ["failure", "list", "--help"],
    ["failure", "inject", "--help"],
    ["failure", "describe", "--help"],
    ["failure", "ps", "--help"],
    ["session", "--help"],
    ["session", "ps", "--help"],
    ["session", "inspect", "--help"],
    ["session", "containers", "--help"],
    ["session", "close", "--help"],
    ["session", "wipe", "--help"],
    ["remote", "--help"],
    ["remote", "serve", "--help"],
    ["remote", "health", "--help"],
    ["traffic", "--help"],
    ["traffic", "list", "--help"],
    ["traffic", "run", "--help"],
]
CLI_READ_ONLY_ARGS = [
    ["agent", "list"],
    ["env", "list"],
    ["failure", "list"],
    ["traffic", "list"],
    ["session", "ps"],
]


class CliSmokeTest:
    def test_cli_command_modules_import(self) -> None:
        for module_name in CLI_COMMAND_MODULES:
            importlib.import_module(module_name)

    def test_cli_handler_workflow_modules_import(self) -> None:
        for module_name in CLI_HANDLER_WORKFLOWS:
            importlib.import_module(module_name)

    def test_eval_clean_import_regression(self) -> None:
        """``nika eval clean`` imports ``eval.clean`` via the package namespace."""
        importlib.invalidate_caches()
        clean = importlib.import_module("nika.workflows.eval.clean")

        assert callable(clean.run_eval_clean)

    def test_cli_help_invocations(self) -> None:
        for args in CLI_HELP_ARGS:
            result = _RUNNER.invoke(app, args)

            assert result.exit_code == 0

    def test_cli_read_only_invocations(self) -> None:
        for args in CLI_READ_ONLY_ARGS:
            result = _RUNNER.invoke(app, args)

            assert result.exit_code == 0

    def test_env_static_validation_is_opt_in(self) -> None:
        with patch(
            "nika.workflows.env.start.start_net_env", return_value="session-test"
        ) as mocked:
            default = _RUNNER.invoke(app, ["env", "run", "simple_bgp"])
            assert default.exit_code == 0, default.output
            assert mocked.call_args.kwargs["static_validation"] is None

            enabled = _RUNNER.invoke(
                app, ["env", "run", "simple_bgp", "--static-validation"]
            )
            assert enabled.exit_code == 0, enabled.output
            assert mocked.call_args.kwargs["static_validation"] is True

    def test_benchmark_run_defaults_to_candidate_catalog(self) -> None:
        with patch(
            "nika.cli.commands.benchmark.run_benchmark_from_yaml"
        ) as run_from_yaml:
            result = _RUNNER.invoke(app, ["benchmark", "run"])
        assert result.exit_code == 0, result.output
        assert run_from_yaml.call_args.kwargs["benchmark_file"].endswith(
            "benchmark/working/pool"
        )

        both = _RUNNER.invoke(
            app,
            [
                "benchmark",
                "run",
                "--config",
                "benchmark/working/pool",
                "--release",
                "0.1.0",
            ],
        )
        assert both.exit_code != 0
        both_text = f"{both.output}\n{both.stderr or ''}"
        assert "--config" in both_text and "--release" in both_text

    def test_benchmark_run_help_includes_split(self) -> None:
        result = _RUNNER.invoke(app, ["benchmark", "run", "--help"])
        assert result.exit_code == 0, result.output
        assert "--split" in result.output

    def test_benchmark_run_forwards_split(self) -> None:
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
                    "dev",
                    "--result_dir",
                    "/tmp/nika-split-smoke",
                ],
            )
        assert result.exit_code == 0, result.output
        assert run_from_release.call_args.kwargs["split"] == "dev"
        assert run_from_release.call_args.kwargs["continue_on_error"] is True

    def test_console_script_help_invocations(self) -> None:
        assert (_REPO_ROOT / "src" / "nika").is_dir(), _REPO_ROOT
        for args in CLI_HELP_ARGS:
            completed = subprocess.run(
                [sys.executable, "-m", "nika.cli.main", *args],
                cwd=_REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            assert completed.returncode == 0, (
                f"args={args!r} cwd={_REPO_ROOT}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
