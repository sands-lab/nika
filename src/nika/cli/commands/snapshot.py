"""Commands for golden VM snapshots (advanced ``nika.snapshot`` mode)."""

from __future__ import annotations

import typer

from nika.run_config.loader import ENV_RUN_CONFIG

snapshot_app = typer.Typer(
    help="Golden VM snapshots of labs for nika.snapshot benchmark runs."
)


def _load_run_config(run_config: str | None) -> None:
    from nika.run_config.loader import export_run_config_env

    export_run_config_env(run_config)


@snapshot_app.command("list")
def snapshot_list(
    run_config: str | None = typer.Option(
        None, "--run-config", envvar=ENV_RUN_CONFIG, help="Path to config/nika.yaml."
    ),
) -> None:
    """List golden VMs and running trial copies."""
    _load_run_config(run_config)
    from nika.snapshot.golden import list_goldens
    from nika.snapshot.trial import trial_rows

    goldens = list_goldens()
    if not goldens:
        typer.echo("No golden VMs.")
    for row in goldens:
        state = (
            "ready" if row["ready"] else "building" if row["building"] else "incomplete"
        )
        if row["ready"]:
            state += (
                ", current"
                if row["current"]
                else ", stale (code, settings or base changed)"
            )
        typer.echo(f"{row['key']}  {state}  {row['variant']}")
    for row in trial_rows():
        state = "running" if row["alive"] else "orphaned (process gone)"
        typer.echo(
            f"trial copy {row['path'].name}  {state}  trial={row['trial']}"
            f"  results={row['owner']}"
        )


@snapshot_app.command("build")
def snapshot_build(
    release: str = typer.Option(..., "--release", "-d", help="Release version."),
    split: str = typer.Option("dev", "--split", help="Release split: dev or test."),
    scenario: list[str] | None = typer.Option(
        None, "--scenario", help="Only these scenario ids (repeatable)."
    ),
    run_config: str | None = typer.Option(
        None, "--run-config", envvar=ENV_RUN_CONFIG, help="Path to config/nika.yaml."
    ),
) -> None:
    """Build the golden VMs a release split needs, before running it."""
    _load_run_config(run_config)
    from nika.snapshot.golden import ensure_golden, variant_for_row
    from nika.workflows.benchmark.release import load_release

    rows = load_release(release, split=split).cases
    variants: list[dict] = []
    for row in rows:
        if scenario and row["scenario"] not in scenario:
            continue
        params = variant_for_row(row)
        if params not in variants:
            variants.append(params)
    typer.echo(f"{len(variants)} environment variant(s) for {release}/{split}.")
    for params in variants:
        golden = ensure_golden(params)
        typer.echo(f"{golden.key}  {params}")


@snapshot_app.command("clean")
def snapshot_clean(
    all_goldens: bool = typer.Option(
        False, "--all", help="Delete every golden VM, not only stale or abandoned ones."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    run_config: str | None = typer.Option(
        None, "--run-config", envvar=ENV_RUN_CONFIG, help="Path to config/nika.yaml."
    ),
) -> None:
    """Release orphaned trial copies; delete stale or abandoned golden VMs.

    Golden VMs still being built or used by a running trial are kept.
    """
    _load_run_config(run_config)
    from nika.snapshot.golden import delete_golden, list_goldens
    from nika.snapshot.trial import reap_orphan_trials, trial_rows

    for trial in reap_orphan_trials():
        typer.echo(f"Released orphaned trial copy of {trial}.")

    # Never delete a golden VM that any checkout is building or restoring from.
    in_use = {row["golden"] for row in trial_rows() if row["alive"]}
    targets = [
        row
        for row in list_goldens()
        if not row["building"]
        and row["key"] not in in_use
        and (all_goldens or not row["current"] or not row["ready"])
    ]
    if not targets:
        typer.echo("No golden VMs to delete.")
        return
    typer.echo("\n".join(row["key"] for row in targets))
    if not yes:
        typer.confirm(f"Delete {len(targets)} golden VM(s)?", abort=True)
    for row in targets:
        delete_golden(row)
    typer.echo(f"Deleted {len(targets)} golden VM(s).")
