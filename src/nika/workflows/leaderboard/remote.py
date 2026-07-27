"""Constants and path helpers for the public NIKA leaderboard GitHub repo."""

from __future__ import annotations

DEFAULT_LEADERBOARD_REPO = "sands-lab/nika-leaderboard"
DEFAULT_LEADERBOARD_HTTPS = "https://github.com/sands-lab/nika-leaderboard.git"
DEFAULT_LEADERBOARD_SSH = "git@github.com:sands-lab/nika-leaderboard.git"

SUBMISSIONS_DIRNAME = "submissions"


def remote_submission_relpath(release_version: str, package_name: str) -> str:
    """Return repo-relative path for a packed submission directory."""
    version = release_version.strip().strip("/")
    name = package_name.strip().strip("/")
    if not version or not name:
        raise ValueError("release_version and package_name must be non-empty")
    if "/" in version or "/" in name or "\\" in version or "\\" in name:
        raise ValueError(
            f"invalid remote path components: version={release_version!r} "
            f"package={package_name!r}"
        )
    return f"{SUBMISSIONS_DIRNAME}/{version}/{name}"


def submission_branch_name(package_name: str) -> str:
    """Branch name used when opening a submission PR."""
    name = package_name.strip().strip("/")
    if not name:
        raise ValueError("package_name must be non-empty")
    return f"submission/{name}"
