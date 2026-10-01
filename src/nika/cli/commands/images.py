"""nika images: prepare and list the Docker images NIKA uses."""

from __future__ import annotations

import typer

images_app = typer.Typer(help="Prepare and list NIKA Docker images and caches.")


@images_app.command("prepare")
def images_prepare(
    force_rebuild: bool = typer.Option(
        False, "--force-rebuild", help="Rebuild every local nika/* image."
    ),
) -> None:
    """Build, pull, and cache every image benchmarks deploy (vendor images excluded).

    Outdated nika/* builds are rebuilt, k8s workload archives and Helm charts
    are cached, and leftovers from older NIKA releases are removed.
    """
    from nika.workflows.setup.images import prepare_all_images

    prepare_all_images(force_rebuild=force_rebuild)


@images_app.command("list")
def images_list(
    owned: bool = typer.Option(
        False,
        "--owned",
        help=(
            "List every image NIKA may create or pull, including vendor, build "
            "parent, and legacy images (used by scripts/uninstall.sh)."
        ),
    ),
) -> None:
    """Print the images `nika images prepare` ensures, one per line."""
    from nika.workflows.setup.images import (
        legacy_images,
        owned_images,
        runtime_images,
    )

    if owned:
        images = sorted(set(owned_images()) | set(legacy_images()))
    else:
        images = runtime_images()
    for image in images:
        typer.echo(image)
