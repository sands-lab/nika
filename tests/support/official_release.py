"""Offline stand-in for the official release tags and files on GitHub."""

from __future__ import annotations

import hashlib
from pathlib import Path

from nika.workflows.benchmark import official_release
from nika.workflows.benchmark.official_release import (
    RELEASES_RELPATH,
    OfficialReleaseUnavailable,
)
from nika.workflows.benchmark.release import releases_dir


class FakeOfficialRepo:
    """Serve ``release/<version>`` tags and commit contents from memory.

    Untagged versions resolve to the current local release files, so tests that
    point ``RELEASES_DIR`` at a temporary release validate as official.
    """

    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.commits: dict[str, dict[str, bytes]] = {}
        self.unreachable = False

    def publish(self, version: str, files: dict[str, bytes]) -> str:
        """Store ``files`` (name → bytes) of ``version`` as a commit; return its SHA."""
        digest = hashlib.sha1(usedforsecurity=False)
        for name in sorted(files):
            digest.update(name.encode() + b"\0" + files[name])
        commit = digest.hexdigest()
        self.commits[commit] = {
            f"{RELEASES_RELPATH}/{version}/{name}": data for name, data in files.items()
        }
        return commit

    def publish_dir(self, version: str, root: Path) -> str:
        return self.publish(
            version,
            {path.name: path.read_bytes() for path in root.glob("*.yaml")},
        )

    def tag(self, version: str, commit: str) -> None:
        self.tags[version] = commit

    def resolve(self, version: str, *, repo: str = "") -> str:
        if self.unreachable:
            raise OfficialReleaseUnavailable("cannot read tags: network unreachable")
        if version in self.tags:
            return self.tags[version]
        local = releases_dir() / version
        if not local.is_dir():
            raise OfficialReleaseUnavailable(f"no tag release/{version}")
        return self.publish_dir(version, local)

    def read(self, commit: str, relpath: str, *, repo: str) -> bytes:
        if self.unreachable:
            raise OfficialReleaseUnavailable(f"cannot fetch {relpath}: unreachable")
        try:
            return self.commits[commit][relpath]
        except KeyError:
            raise OfficialReleaseUnavailable(
                f"cannot fetch {relpath} at {commit}: 404 Not Found"
            ) from None


def install(monkeypatch) -> FakeOfficialRepo:
    fake = FakeOfficialRepo()
    official_release._release_files_at.cache_clear()
    official_release._MATERIALIZED.clear()
    monkeypatch.setattr(official_release, "resolve_release_commit", fake.resolve)
    monkeypatch.setattr(official_release, "_read_release_file", fake.read)
    return fake
