"""Download a submission's trajectories from the Hugging Face dataset.

A leaderboard submission opens two pull requests: the scores package on
GitHub and the trajectories on the Hub (``nika leaderboard submit``). The
leaderboard CI sees only the first, so the audit fetches the trajectories
itself: from ``main`` once merged, or from the open Hub pull request titled
``Add trajectories trajectories/<release>/<slug>``.
"""

from __future__ import annotations

from pathlib import Path

from nika.workflows.leaderboard.hf_remote import (
    DEFAULT_TRAJECTORIES_REPO,
    remote_trajectories_relpath,
)


class TrajectoryFetchError(RuntimeError):
    pass


def find_revision(
    slug: str, release: str, *, repo: str = DEFAULT_TRAJECTORIES_REPO
) -> str:
    """``main`` if the package is merged, else the open Hub PR's ``refs/pr/N``."""
    from huggingface_hub import HfApi

    api = HfApi()
    rel = remote_trajectories_relpath(release, slug)
    try:
        if any(
            f.startswith(rel + "/")
            for f in api.list_repo_files(repo, repo_type="dataset")
        ):
            return "main"
    except Exception as exc:  # noqa: BLE001 - surface any Hub error the same way
        raise TrajectoryFetchError(f"cannot list {repo}: {exc}") from exc
    for disc in api.get_repo_discussions(repo, repo_type="dataset"):
        if (
            disc.is_pull_request
            and disc.status == "open"
            and disc.title.split()[-1:] == [rel]
        ):
            return f"refs/pr/{disc.num}"
    raise TrajectoryFetchError(
        f"no trajectories for {rel} on main or in an open PR of {repo}"
    )


def fetch_trajectories(
    slug: str,
    release: str,
    dest: Path,
    *,
    repo: str = DEFAULT_TRAJECTORIES_REPO,
    revision: str | None = None,
) -> Path:
    """Download ``trajectories/<release>/<slug>`` into *dest*; return its path."""
    from huggingface_hub import snapshot_download

    rel = remote_trajectories_relpath(release, slug)
    revision = revision or find_revision(slug, release, repo=repo)
    snapshot_download(
        repo_id=repo,
        repo_type="dataset",
        revision=revision,
        allow_patterns=[f"{rel}/*", f"{rel}/**"],
        local_dir=str(dest),
    )
    path = Path(dest) / rel
    if not path.is_dir():
        raise TrajectoryFetchError(f"download of {rel}@{revision} produced no files")
    return path
