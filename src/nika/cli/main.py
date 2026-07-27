"""Root Typer application for the ``nika`` console script (``nika.cli``)."""

import nika.config  # noqa: F401 — load .env before Typer reads envvar defaults

import typer

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
            "nika.cli.commands.benchmark", "benchmark_app", "Batch benchmark runs."
        ),
        "leaderboard": LazyCommandSpec(
            "nika.cli.commands.leaderboard",
            "leaderboard_app",
            "Pack and validate leaderboard submissions.",
        ),
        "traffic": LazyCommandSpec(
            "nika.cli.commands.traffic",
            "traffic_app",
            "Generate traffic in the Kathará lab.",
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
    app()


if __name__ == "__main__":
    main()
