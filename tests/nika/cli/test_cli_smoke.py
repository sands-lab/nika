from __future__ import annotations
import importlib
import subprocess
import sys
from pathlib import Path
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
    "nika.cli.commands.env",
    "nika.cli.commands.evaluation",
    "nika.cli.commands.exec",
    "nika.cli.commands.failure",
    "nika.cli.commands.session",
    "nika.cli.commands.traffic",
]
CLI_HANDLER_WORKFLOWS = [
    "nika.workflows.agent.run",
    "nika.workflows.benchmark.run",
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

    def test_benchmark_run_requires_config_or_release(self) -> None:
        """Bare ``nika benchmark run`` has no default suite."""
        result = _RUNNER.invoke(app, ["benchmark", "run"])
        assert result.exit_code != 0
        combined = f"{result.output}\n{result.stderr or ''}"
        assert "--config" in combined
        assert "--release" in combined

        result_dir_only = _RUNNER.invoke(
            app, ["benchmark", "run", "--result_dir", "results/tmp"]
        )
        assert result_dir_only.exit_code != 0
        combined_dir = f"{result_dir_only.output}\n{result_dir_only.stderr or ''}"
        assert "no default" in combined_dir.lower() or "--release" in combined_dir

        both = _RUNNER.invoke(
            app,
            [
                "benchmark",
                "run",
                "--config",
                "benchmark/benchmark_selected.yaml",
                "--release",
                "0.1.0",
            ],
        )
        assert both.exit_code != 0
        both_text = f"{both.output}\n{both.stderr or ''}"
        assert "--config" in both_text and "--release" in both_text

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
