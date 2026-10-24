"""Select benchmark rows by device profile and check licensed vendor images.

Rows with an IOS XR or RouterOS ``device_profile`` (and the licensed
``iosxr_simple_bgp`` / ``routeros_simple_bgp`` scenarios) need vendor images
that NIKA cannot download. Runs check them before any lab starts, so a
missing image is reported with the affected rows instead of failing inside
deployment.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

PREPARE_HINT = (
    "Prepare them with `./scripts/install.sh --with-vendor-images` (see "
    "docs/operations/network-scenarios.md), or leave these rows out with "
    "--skip-licensed or --device-profile."
)


def row_device_profile(row: dict[str, Any]) -> str:
    """Router platform of a row; rows without one deploy FRR."""
    return str(row.get("device_profile") or "frr")


def row_licensed_images(row: dict[str, Any]) -> tuple[str, ...]:
    from nika.net_env.net_env_pool import deployment_licensed_images

    try:
        return deployment_licensed_images(row["scenario"], row.get("device_profile"))
    except (KeyError, ValueError):
        return ()


def select_rows(
    rows: Iterable[dict[str, Any]],
    *,
    skip_licensed: bool = False,
    device_profiles: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep rows whose platform is listed and, optionally, that need no license."""
    wanted = {str(p).strip().lower() for p in device_profiles or ()}
    return [
        row
        for row in rows
        if (not wanted or row_device_profile(row) in wanted)
        and not (skip_licensed and row_licensed_images(row))
    ]


def missing_licensed_images(rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """Map each licensed image absent locally to the rows (labels) that need it."""
    from nika.net_env.utils.kathara.docker_files.docker_images import image_exists

    needed: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for image in row_licensed_images(row):
            label = f"{row['scenario']}/{row_device_profile(row)}"
            needed[image].append(label)
    return {
        image: labels for image, labels in needed.items() if not image_exists(image)
    }


def require_licensed_images(rows: Iterable[dict[str, Any]]) -> None:
    """Raise ``ValueError`` naming rows whose licensed images are not installed."""
    missing = missing_licensed_images(rows)
    if not missing:
        return
    lines = ["These rows need licensed vendor images that are not installed:"]
    for image, labels in sorted(missing.items()):
        counts: dict[str, int] = defaultdict(int)
        for label in labels:
            counts[label] += 1
        detail = ", ".join(f"{label} x{n}" for label, n in sorted(counts.items()))
        lines.append(f"  {image}: {len(labels)} rows ({detail})")
    lines.append(PREPARE_HINT)
    raise ValueError("\n".join(lines))
