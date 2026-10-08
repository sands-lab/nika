"""Interactive discovery and manual experiments from existing case catalogs."""

import json
import shlex
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console

from nika.workflows.case import DEFAULT_CATALOG, DEPLOY_KEYS, load_example_cases
from nika.workflows.benchmark.load_config import normalize_benchmark_row
from nika.workflows.benchmark.trials import (
    describe_task_payload,
    resolve_catalog_row,
    task_id_for_row,
)

case_app = typer.Typer(
    help="Browse example cases and start manual troubleshooting labs."
)


def _deployment(row: dict[str, Any]) -> str:
    return (
        " / ".join(
            f"{key}={row[key]}" for key in DEPLOY_KEYS if row.get(key) not in (None, "")
        )
        or "Scenario defaults"
    )


def _preview(row: dict[str, Any]) -> str:
    return (
        f"Environment: {row['scenario']}\n"
        f"Deployment:  {_deployment(row)}\n"
        f"Failure:     {row['problem']}\n"
        f"Injection:   {', '.join(f'{k}={v}' for k, v in row['inject'].items())}\n"
        f"Task ID:     {task_id_for_row(row)}\n"
        "Source: catalog preset; E2E verification status is not recorded."
    )


def _commands(row: dict[str, Any], catalog: Path) -> None:
    typer.echo(
        shlex.join(
            [
                "nika",
                "case",
                "run",
                task_id_for_row(row),
                "--catalog",
                str(catalog),
            ]
        )
    )


def _export(row: dict[str, Any], path: Path) -> None:
    # Exclusive creation protects user files; normalize strips pool-only metadata.
    with path.open("x", encoding="utf-8") as output:
        yaml.safe_dump(
            {"cases": [normalize_benchmark_row(row)]}, output, sort_keys=False
        )
    typer.echo(f"Saved {path}")
    typer.echo(shlex.join(["nika", "benchmark", "run", "--config", str(path)]))


def _start(row: dict[str, Any], result_dir: str | None) -> None:
    from nika.workflows.case import start_example_case

    session_id = start_example_case(row, result_dir=result_dir)
    typer.echo(f"session_id={session_id}")
    typer.echo("The lab is running with the selected failure. No agent was started.")
    host = row["inject"].get("host_name")
    if host:
        typer.echo(
            shlex.join(
                [
                    "nika",
                    "exec",
                    "--session_id",
                    session_id,
                    host,
                    "ip link show",
                ]
            )
        )
    typer.echo(shlex.join(["nika", "session", "close", "--session_id", session_id]))


def _browse(
    rows: list[dict[str, Any]], output: Path | None
) -> tuple[dict[str, Any], str] | None:
    from nika.cli.prompts import choose
    from nika.problems.registry import get_problem_class

    selections: list[str | None] = [None] * 5
    step = 0
    direction = 1
    while 0 <= step < 5:
        candidates = rows
        if step > 0:
            candidates = [row for row in candidates if row["scenario"] == selections[0]]
        if step > 1:
            candidates = [row for row in candidates if row["problem"] == selections[1]]
        if step > 2:
            candidates = [
                row for row in candidates if _deployment(row) == selections[2]
            ]
        if step > 3:
            candidates = [
                row for row in candidates if task_id_for_row(row) == selections[3]
            ]

        context = " > ".join(value for value in selections[: min(step, 3)] if value)
        if step == 0:
            names = sorted({row["scenario"] for row in candidates})
            choices = [
                (
                    name,
                    f"{name}  ({len({row['problem'] for row in candidates if row['scenario'] == name})} failures)",
                )
                for name in names
            ]
            title = "Choose an environment"
        elif step == 1:
            choices = []
            for problem in sorted({row["problem"] for row in candidates}):
                cls = get_problem_class(problem, selections[0])
                description = " ".join(
                    str(getattr(cls, "description", "") or "").split()
                )
                choices.append((problem, f"{problem}  {description[:120]}"))
            title = "Choose a failure"
        elif step == 2:
            # Catalog order puts smaller topologies first. Keep full profiles intact.
            profiles = list(dict.fromkeys(_deployment(row) for row in candidates))
            choices = [
                (profile, profile + ("  (default)" if i == 0 else ""))
                for i, profile in enumerate(profiles)
            ]
            title = "Choose a deployment"
        elif step == 3:
            choices = [
                (
                    task_id_for_row(row),
                    ", ".join(f"{k}={v}" for k, v in row["inject"].items()),
                )
                for row in candidates
            ]
            title = "Choose an injection preset"
        else:
            title = _preview(candidates[0]) + "\n\nChoose an action"
            choices = (
                [("export", f"Export case YAML to {output}")]
                if output
                else [
                    ("start", "Start lab and inject failure"),
                    ("export", "Export case YAML"),
                    ("commands", "Show reproduction command"),
                ]
            )

        # Skip unnecessary questions, but keep Back usable across skipped steps.
        if len(choices) == 1 and step < 4:
            value = choices[0][0]
            if selections[step] != value:
                selections[step + 1 :] = [None] * (4 - step)
            selections[step] = value
            step += direction
            continue
        value = choose(
            f"{context}\n{title}" if context else title,
            choices,
            default=selections[step],
        )
        if value is None:
            direction = -1
            step -= 1
            continue
        if selections[step] != value:
            selections[step + 1 :] = [None] * (4 - step)
        selections[step] = value
        direction = 1
        if step == 4:
            return candidates[0], value
        step += 1
    return None


@case_app.command("browse")
def case_browse(
    env: str | None = typer.Option(None, "--env", help="Preselect an environment."),
    failure: str | None = typer.Option(None, "--failure", help="Preselect a failure."),
    catalog: Path = typer.Option(
        DEFAULT_CATALOG,
        "--catalog",
        help="Working pool, candidate YAML, or flat cases YAML.",
    ),
    task_id: str | None = typer.Option(
        None, "--task-id", help="Select a complete preset by task ID."
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help="Export the selected case to a new YAML file."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="List matching presets as JSON without prompting."
    ),
    result_dir: str | None = typer.Option(
        None, "--result-dir", help="Results parent directory when starting a lab."
    ),
) -> None:
    """Search environments, failures, deployment profiles, and injection presets."""
    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        with (
            Console(stderr=True).status("Loading example cases...")
            if interactive
            else nullcontext()
        ):
            rows = load_example_cases(catalog)
        rows = [
            row
            for row in rows
            if (env is None or row["scenario"] == env)
            and (failure is None or row["problem"] == failure)
        ]
        if task_id:
            rows = [resolve_catalog_row(rows, task_id)]
        if not rows:
            raise ValueError("No matching single-fault presets in this catalog.")
        if as_json:
            if output is not None:
                raise ValueError("Use either --json or --output.")
            typer.echo(
                json.dumps(
                    [
                        {
                            "e2e_verification": "not_recorded",
                            **describe_task_payload(row),
                        }
                        for row in rows
                    ],
                    indent=2,
                )
            )
            return
        if not interactive:
            if len(rows) != 1:
                raise ValueError(
                    "Interactive browsing requires a terminal. Use --json to list presets, then --task-id to select one."
                )
            row = rows[0]
            action = "export" if output else "commands"
        else:
            selected = _browse(rows, output)
            if selected is None:
                typer.echo("Cancelled.")
                return
            row, action = selected
        typer.echo(_preview(row))
        if action == "start":
            _start(row, result_dir)
        elif action == "export":
            path = output or Path(
                typer.prompt("Output YAML path", default=f"{task_id_for_row(row)}.yaml")
            )
            _export(row, path)
        else:
            _commands(row, catalog)
    except KeyboardInterrupt:
        typer.echo("Cancelled.")
        raise typer.Exit(130) from None
    except (OSError, ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@case_app.command("run")
def case_run(
    task_id: str = typer.Argument(..., help="Task ID from `nika case browse`."),
    catalog: Path = typer.Option(
        DEFAULT_CATALOG, "--catalog", help="Catalog containing this preset."
    ),
    result_dir: str | None = typer.Option(
        None, "--result-dir", help="Results parent directory."
    ),
) -> None:
    """Start an isolated lab and inject one catalog preset, without running an agent."""
    try:
        row = resolve_catalog_row(load_example_cases(catalog), task_id)
        typer.echo(_preview(row))
        _start(row, result_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
