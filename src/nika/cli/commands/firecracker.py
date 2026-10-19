"""Commands for Firecracker lab images (``nika.execution: firecracker``)."""

from __future__ import annotations

import typer

from nika.run_config.loader import ENV_RUN_CONFIG

firecracker_app = typer.Typer(
    help="Lab images for benchmark runs in Firecracker (nika.execution: firecracker)."
)


def _load_run_config(run_config: str | None) -> None:
    from nika.run_config.loader import export_run_config_env

    export_run_config_env(run_config)


@firecracker_app.command("list")
def snapshot_list(
    run_config: str | None = typer.Option(
        None, "--run-config", envvar=ENV_RUN_CONFIG, help="Path to config/nika.yaml."
    ),
) -> None:
    """List lab images and running trial copies."""
    _load_run_config(run_config)
    from nika.firecracker.image import list_images
    from nika.firecracker.trial import trial_rows

    images = list_images()
    if not images:
        typer.echo("No lab images.")
    for row in images:
        state = (
            "ready" if row["ready"] else "building" if row["building"] else "incomplete"
        )
        if row["ready"]:
            state += (
                ", current"
                if row["current"]
                else ", stale (code, settings or base changed)"
            )
        memory = row.get("guest_memory_mib") or {}
        used = f"  uses {memory['used']}/{memory['total']} MiB" if memory else ""
        typer.echo(f"{row['key']}  {state}{used}  {row['variant']}")
    for row in trial_rows():
        state = "running" if row["alive"] else "orphaned (process gone)"
        typer.echo(
            f"trial copy {row['path'].name}  {state}  trial={row['trial']}"
            f"  results={row['owner']}"
        )


@firecracker_app.command("clean")
def snapshot_clean(
    all_images: bool = typer.Option(
        False, "--all", help="Delete every lab image, not only stale or abandoned ones."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    run_config: str | None = typer.Option(
        None, "--run-config", envvar=ENV_RUN_CONFIG, help="Path to config/nika.yaml."
    ),
) -> None:
    """Release orphaned trial copies; delete stale or abandoned lab images.

    LabImage VMs still being built or used by a running trial are kept.
    """
    _load_run_config(run_config)
    from nika.firecracker.image import delete_image, list_images
    from nika.firecracker.trial import reap_orphan_trials, trial_rows

    for trial in reap_orphan_trials():
        typer.echo(f"Released orphaned trial copy of {trial}.")

    # Never delete a lab image that any checkout is building or restoring from.
    in_use = {row["image"] for row in trial_rows() if row["alive"]}
    targets = [
        row
        for row in list_images()
        if not row["building"]
        and row["key"] not in in_use
        and (all_images or not row["current"] or not row["ready"])
    ]
    if not targets:
        typer.echo("No lab images to delete.")
        return
    typer.echo("\n".join(row["key"] for row in targets))
    if not yes:
        typer.confirm(f"Delete {len(targets)} lab image(s)?", abort=True)
    for row in targets:
        delete_image(row)
    typer.echo(f"Deleted {len(targets)} lab image(s).")
