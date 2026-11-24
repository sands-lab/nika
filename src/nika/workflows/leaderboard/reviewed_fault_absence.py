"""Maintainer reviews bound to exact score and trajectory packages."""

from hashlib import file_digest, sha256
from pathlib import Path

from nika.workflows.leaderboard.issues import ValidationIssue

_REVIEWS = {
    # Trajectory PR #32 at Hub revision 2e46bdb38c5ccb31fb5f9bbd0ae32d8d3d2b63e1.
    "20261123_react_deepseek_deepseek_v4_pro_0813": (
        "a6125719208a779be08d0e8be57b7edba06b84d7bf3b248c1a4783ef2541da4b",
        "9c770e36e33de6488cc5e341904aec5162572ff9821cf733fe7d24fb51f2d003",
        frozenset(
            {
                "campus_lan__dns_service_down__h88cd16fc8e7e__t01",
                "campus_lan__dns_service_down__h88cd16fc8e7e__t02",
                "campus_lan__dns_service_down__h88cd16fc8e7e__t03",
            }
        ),
        "https://github.com/sands-lab/nika-leaderboard/pull/99#issuecomment-7104822287",
    )
}


def _package_digest(root: Path) -> str:
    digest = sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with path.open("rb") as stream:
                digest.update(file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def review_fault_absences(
    scores_root: Path | None, trajectories_root: Path, absent: set[str]
) -> ValidationIssue | None:
    """Return a finding for the observed, byte-matched slots in a trusted review."""
    if scores_root is None or not absent:
        return None
    review = _REVIEWS.get(scores_root.name)
    if review is None:
        return None
    scores_digest, trajectories_digest, reviewed, evidence = review
    matched = absent & reviewed
    if not matched:
        return None
    try:
        if _package_digest(scores_root) != scores_digest:
            return None
        if _package_digest(trajectories_root) != trajectories_digest:
            return None
    except OSError:
        return None
    return ValidationIssue(
        "WARNING_REVIEWED_FAULT_ABSENCE",
        items=sorted(matched),
        details=[f"Maintainer evidence: {evidence}"],
    )
