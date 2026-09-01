"""Constants and path helpers for the public NIKA trajectories HF dataset."""

from __future__ import annotations

DEFAULT_TRAJECTORIES_REPO = "Zhihao98/nika-trajectories"
DEFAULT_TRAJECTORIES_URL = (
    f"https://huggingface.co/datasets/{DEFAULT_TRAJECTORIES_REPO}"
)

TRAJECTORIES_DIRNAME = "trajectories"


def remote_trajectories_relpath(release_version: str, package_name: str) -> str:
    """Return dataset-relative path for a packed trajectories directory.

    ``package_name`` is the scores package dirname (without ``_trajectories``).
    """
    version = release_version.strip().strip("/")
    name = package_name.strip().strip("/")
    if name.endswith("_trajectories"):
        name = name[: -len("_trajectories")]
    if not version or not name:
        raise ValueError("release_version and package_name must be non-empty")
    if "/" in version or "/" in name or "\\" in version or "\\" in name:
        raise ValueError(
            f"invalid remote path components: version={release_version!r} "
            f"package={package_name!r}"
        )
    return f"{TRAJECTORIES_DIRNAME}/{version}/{name}"


def trajectories_browser_url(
    release_version: str,
    package_name: str,
    *,
    repo: str = DEFAULT_TRAJECTORIES_REPO,
) -> str:
    """Public Hub tree URL for a merged trajectories package."""
    rel = remote_trajectories_relpath(release_version, package_name)
    return f"https://huggingface.co/datasets/{repo}/tree/main/{rel}"
