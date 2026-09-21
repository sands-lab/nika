"""Root Typer application for the ``nika`` console script (``nika.cli``)."""

import typer

import nika.config  # noqa: F401 — load .env before Typer reads envvar defaults
from nika.cli.lazy_group import LAZY_COMMANDS, LazyCommandSpec, LazyTyperGroup

LAZY_COMMANDS.update(
    {
        "session": LazyCommandSpec(
            "nika.cli.commands.session", "session_app", "Session lifecycle."
        ),
        "env": LazyCommandSpec(
            "nika.cli.commands.env", "env_app", "Deploy and manage network scenarios."
        ),
        "failure": LazyCommandSpec(
            "nika.cli.commands.failure", "failure_app", "Inject and inspect faults."
        ),
        "exec": LazyCommandSpec(
            "nika.cli.commands.exec",
            "exec_app",
            "Execute a shell command inside a host.",
        ),
        "agent": LazyCommandSpec(
            "nika.cli.commands.agent", "agent_app", "Troubleshooting agents."
        ),
        "eval": LazyCommandSpec(
            "nika.cli.commands.evaluation", "eval_app", "Evaluate agent runs."
        ),
        "benchmark": LazyCommandSpec(
            "nika.cli.commands.benchmark", "benchmark_app", "Run benchmark cases."
        ),
        "leaderboard": LazyCommandSpec(
            "nika.cli.commands.leaderboard",
            "leaderboard_app",
            "Pack, validate, and submit leaderboard entries.",
        ),
        "traffic": LazyCommandSpec(
            "nika.cli.commands.traffic",
            "traffic_app",
            "Generate traffic in the Kathará lab.",
        ),
        "remote": LazyCommandSpec(
            "nika.cli.commands.remote",
            "remote_app",
            "Optional remote lab-host control plane.",
        ),
        "config": LazyCommandSpec(
            "nika.cli.commands.config_cmd",
            "config_app",
            "Run configuration (config/nika.yaml).",
        ),
        "inspect": LazyCommandSpec(
            "nika.cli.commands.inspect_cmd",
            "inspect_app",
            "Browse session trajectories in a local web UI.",
        ),
    }
)

app = typer.Typer(
    cls=LazyTyperGroup,
    help="NIKA network troubleshooting pipeline CLI.",
)


@app.callback()
def _root() -> None:
    """NIKA network troubleshooting pipeline CLI."""


def main() -> None:
    """Console entrypoint for setuptools `[project.scripts]`."""
    # Before lazy command imports pull MCP FastMCP / pydantic_settings.
    # Do not quiet third-party loggers here — only ``nika benchmark run`` does.
    from nika.cli.warning_capture import (
        install_warning_capture,
        print_deferred_warnings,
    )

    install_warning_capture(quiet_loggers=False)
    try:
        app()
    finally:
        # Flush any warnings buffered for non-benchmark commands (benchmark
        # run also prints them after its report; a second call is a no-op).
        print_deferred_warnings()


if __name__ == "__main__":
    main()
