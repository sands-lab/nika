"""Commands for Firecracker lab images (``nika.execution.mode: firecracker``)."""

from __future__ import annotations

import typer

from nika.run_config.loader import ENV_RUN_CONFIG

firecracker_app = typer.Typer(
    help="Lab images for benchmark runs in Firecracker (nika.execution.mode: firecracker)."
)


def _load_run_config(run_config: str | None) -> None:
    from nika.run_config.loader import export_run_config_env

    export_run_config_env(run_config)


@firecracker_app.command("list")
def images_list(
    run_config: str | None = typer.Option(
        None, "--run-config", envvar=ENV_RUN_CONFIG, help="Path to config/nika.yaml."
    ),
) -> None:
    """List lab images, case images and running trial copies."""
    _load_run_config(run_config)
    from nika.firecracker.case import list_cases
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
    for row in list_cases():
        state = (
            "ready" if row["ready"] else "building" if row["building"] else "incomplete"
        )
        if row["ready"]:
            state += ", current" if row["current"] else ", stale (lab image changed)"
            state += f"  diff {row['diff_bytes'] / 2**20:.0f} MiB  image={row['image']}"
        typer.echo(f"case {row['key']}  {state}  {row.get('problem') or ''}")
    for row in trial_rows():
        state = "running" if row["alive"] else "orphaned (process gone)"
        typer.echo(
            f"trial copy {row['path'].name}  {state}  trial={row['trial']}"
            f"  results={row['owner']}"
        )


@firecracker_app.command("clean")
def images_clean(
    all_images: bool = typer.Option(
        False,
        "--all",
        help="Delete every lab and case image, not only stale or abandoned ones.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    run_config: str | None = typer.Option(
        None, "--run-config", envvar=ENV_RUN_CONFIG, help="Path to config/nika.yaml."
    ),
) -> None:
    """Release orphaned trial copies; delete stale or abandoned lab and case images.

    Images still being built or used by a running trial are kept.
    """
    _load_run_config(run_config)
    from nika.firecracker.case import delete_case, list_cases
    from nika.firecracker.image import delete_image, list_images
    from nika.firecracker.trial import reap_orphan_trials, trial_rows

    for trial in reap_orphan_trials():
        typer.echo(f"Released orphaned trial copy of {trial}.")

    # Never delete a lab image that any checkout is building or restoring from.
    alive = [row for row in trial_rows() if row["alive"]]
    in_use = {row["image"] for row in alive} | {row["case"] for row in alive}

    def stale(row: dict) -> bool:
        return (
            not row["building"]
            and row["key"] not in in_use
            and (all_images or not row["current"] or not row["ready"])
        )

    cases = [row for row in list_cases() if stale(row)]
    images = [row for row in list_images() if stale(row)]
    if not cases and not images:
        typer.echo("No lab or case images to delete.")
        return
    typer.echo(
        "\n".join(
            [row["key"] for row in images] + [f"case {row['key']}" for row in cases]
        )
    )
    if not yes:
        typer.confirm(
            f"Delete {len(images)} lab image(s) and {len(cases)} case image(s)?",
            abort=True,
        )
    for row in cases:
        delete_case(row)
    for row in images:
        delete_image(row)
    typer.echo(f"Deleted {len(images)} lab image(s) and {len(cases)} case image(s).")
