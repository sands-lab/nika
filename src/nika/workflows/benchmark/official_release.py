"""Official benchmark release reference: the ``release/<version>`` tag on NIKA.

Leaderboard checks compute expected trials from the release files at the commit
that tag names in :data:`OFFICIAL_REPO`, not from the local checkout, so a local
check and leaderboard CI read the same task definitions. A commit's files never
change, so they are read from the local Git objects when present and from
GitHub otherwise.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml

from nika.config import REPO_ROOT
from nika.workflows.benchmark.release import (
    BenchmarkRelease,
    ReleaseError,
    SplitName,
    load_release,
    load_release_from_dir,
    parse_release_ref,
)
from nika.workflows.benchmark.trials import task_id_for_row

OFFICIAL_REPO = "sands-lab/nika"
RELEASE_TAG_PREFIX = "release/"
RELEASES_RELPATH = "benchmark/releases"
MANIFEST_FILENAME = "RELEASE.yaml"

ReleaseSource = Literal["official", "local"]
RELEASE_SOURCES: tuple[ReleaseSource, ...] = ("official", "local")

# Release settings that change what an agent faces or how it is scored.
_COMPARED_SETTINGS = ("defaults", "scoring", "tools", "resources")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
# Package identities are untrusted input; keep the version a single path part.
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*")
_NETWORK_TIMEOUT_SEC = 30

# Materialized releases stay readable for the life of the process.
_MATERIALIZED: dict[tuple[str, str, str], tempfile.TemporaryDirectory[str]] = {}


class OfficialReleaseUnavailable(ReleaseError):
    """The official release tag or its files could not be read."""


def release_tag(version: str) -> str:
    """Return the Git tag that names the official commit of ``version``."""
    return f"{RELEASE_TAG_PREFIX}{version}"


@dataclass(frozen=True)
class ReleaseReference:
    """Where the release files used for a check came from."""

    version: str
    source: ReleaseSource
    repo: str | None = None
    tag: str | None = None
    commit: str | None = None

    def describe(self) -> str:
        if self.source == "local":
            return (
                f"local checkout {RELEASES_RELPATH}/{self.version} "
                "(not authoritative for leaderboard submissions)"
            )
        return f"{self.repo}@{self.commit} (tag {self.tag})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "repo": self.repo,
            "tag": self.tag,
            "commit": self.commit,
        }


@dataclass(frozen=True)
class ReleaseDrift:
    """Differences between a reference release split and another copy of it."""

    changed_tasks: list[str]
    added_tasks: list[str]
    changed_settings: list[str]

    @property
    def drifted(self) -> bool:
        return bool(self.changed_tasks or self.added_tasks or self.changed_settings)


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def resolve_release_commit(version: str, *, repo: str = OFFICIAL_REPO) -> str:
    """Return the commit SHA that the ``release/<version>`` tag names on GitHub."""
    tag = release_tag(version)
    url = f"https://github.com/{repo}.git"
    try:
        proc = subprocess.run(
            ["git", "ls-remote", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
            capture_output=True,
            text=True,
            timeout=_NETWORK_TIMEOUT_SEC,
            env=_git_env(),
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        raise OfficialReleaseUnavailable(
            f"cannot read tags from {url}: {detail[-1] if detail else exc}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise OfficialReleaseUnavailable(f"cannot read tags from {url}: {exc}") from exc
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref:
            refs[ref.strip()] = sha.strip()
    # An annotated tag lists the tag object and, with ``^{}``, its commit.
    commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not commit:
        raise OfficialReleaseUnavailable(
            f"{repo} has no tag {tag}, so release {version} has no official "
            "reference yet"
        )
    return commit


def _read_release_file(commit: str, relpath: str, *, repo: str) -> bytes:
    """Return one file at ``commit`` from local Git objects or from GitHub."""
    try:
        return subprocess.run(
            ["git", "show", f"{commit}:{relpath}"],
            cwd=REPO_ROOT,
            capture_output=True,
            timeout=_NETWORK_TIMEOUT_SEC,
            env=_git_env(),
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/{relpath}"
    try:
        response = httpx.get(url, timeout=_NETWORK_TIMEOUT_SEC, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise OfficialReleaseUnavailable(f"cannot fetch {url}: {exc}") from exc
    return response.content


@functools.lru_cache(maxsize=32)
def _release_files_at(
    version: str, commit: str, repo: str
) -> tuple[tuple[str, bytes], ...]:
    base = f"{RELEASES_RELPATH}/{version}"
    manifest_bytes = _read_release_file(
        commit, f"{base}/{MANIFEST_FILENAME}", repo=repo
    )
    try:
        manifest = yaml.safe_load(manifest_bytes)
    except yaml.YAMLError as exc:
        raise ReleaseError(f"invalid {MANIFEST_FILENAME} at {commit}: {exc}") from exc
    splits = manifest.get("splits") if isinstance(manifest, dict) else None
    if not isinstance(splits, dict) or not splits:
        raise ReleaseError(f"{MANIFEST_FILENAME} at {commit} declares no splits")
    names: list[str] = []
    for name, meta in splits.items():
        cases_file = str((meta or {}).get("cases_file") or f"{name}.yaml")
        if "/" in cases_file or "\\" in cases_file or cases_file.startswith("."):
            raise ReleaseError(f"invalid cases_file {cases_file!r} at {commit}")
        names.append(cases_file)
    files = [(MANIFEST_FILENAME, manifest_bytes)]
    files.extend(
        (name, _read_release_file(commit, f"{base}/{name}", repo=repo))
        for name in sorted(set(names))
    )
    return tuple(files)


def load_release_at(
    version: str,
    commit: str,
    *,
    split: SplitName | str,
    repo: str = OFFICIAL_REPO,
) -> BenchmarkRelease:
    """Load release ``version`` as committed at ``commit`` in ``repo``."""
    if not _COMMIT_RE.fullmatch(commit or ""):
        raise ReleaseError(f"expected a full commit SHA, got {commit!r}")
    if not _VERSION_RE.fullmatch(version or ""):
        raise ReleaseError(f"invalid release version {version!r}")
    key = (repo, commit, version)
    holder = _MATERIALIZED.get(key)
    if holder is None:
        files = _release_files_at(version, commit, repo)
        holder = tempfile.TemporaryDirectory(prefix="nika-release-")
        root = Path(holder.name) / version
        root.mkdir()
        for name, data in files:
            (root / name).write_bytes(data)
        _MATERIALIZED[key] = holder
    return load_release_from_dir(Path(holder.name) / version, split=split)  # type: ignore[arg-type]


def load_reference_release(
    version: str,
    *,
    split: SplitName | str,
    source: ReleaseSource = "official",
) -> tuple[BenchmarkRelease, ReleaseReference]:
    """Load the release a leaderboard check compares against.

    ``official`` reads the files at the ``release/<version>`` tag. ``local``
    reads ``benchmark/releases/<version>/`` from this checkout and is meant for
    offline development only.
    """
    if source == "local":
        release = load_release(version, split=split)
        return release, ReleaseReference(version=release.version, source="local")
    if source != "official":
        raise ReleaseError(
            f"unknown release source {source!r}; expected one of {RELEASE_SOURCES}"
        )
    _bench_id, selector = parse_release_ref(version)
    if not _VERSION_RE.fullmatch(selector):
        raise ReleaseError(f"invalid release version {selector!r}")
    commit = resolve_release_commit(selector)
    release = load_release_at(selector, commit, split=split)
    return release, ReleaseReference(
        version=release.version,
        source="official",
        repo=OFFICIAL_REPO,
        tag=release_tag(selector),
        commit=commit,
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def release_drift(reference: BenchmarkRelease, other: BenchmarkRelease) -> ReleaseDrift:
    """Compare the task definitions and settings of two copies of a release split.

    ``changed_tasks`` lists reference task ids that are missing from ``other`` or
    defined differently there; ``added_tasks`` lists task ids only ``other`` has.
    """
    ours = {task_id_for_row(row): _canonical(row) for row in reference.cases}
    theirs = {task_id_for_row(row): _canonical(row) for row in other.cases}
    return ReleaseDrift(
        changed_tasks=sorted(
            task_id for task_id, row in ours.items() if theirs.get(task_id) != row
        ),
        added_tasks=sorted(set(theirs) - set(ours)),
        changed_settings=[
            name
            for name in _COMPARED_SETTINGS
            if _canonical(getattr(reference, name)) != _canonical(getattr(other, name))
        ],
    )


def official_release_drift(
    local: BenchmarkRelease,
) -> tuple[ReleaseDrift, ReleaseReference]:
    """Compare a locally loaded release split with its official copy."""
    official, reference = load_reference_release(local.version, split=local.split)
    return release_drift(official, local), reference


def describe_drift(
    local: BenchmarkRelease, drift: ReleaseDrift, reference: ReleaseReference
) -> str:
    """Warning text for a local release that differs from the official one."""
    lines = [
        f"Local release {local.version} ({local.split}) differs from the official "
        f"release ({reference.describe()})."
    ]
    if drift.changed_tasks:
        shown = ", ".join(drift.changed_tasks[:5])
        more = len(drift.changed_tasks) - 5
        lines.append(
            f"  {len(drift.changed_tasks)} tasks have different definitions: {shown}"
            + (f", … (+{more} more)" if more > 0 else "")
        )
    elif drift.added_tasks:
        # A changed task id also lands in added_tasks, so count additions alone.
        lines.append(
            f"  {len(drift.added_tasks)} tasks are not in the official release."
        )
    if drift.changed_settings:
        lines.append(f"  Changed settings: {', '.join(drift.changed_settings)}.")
    lines.append(
        "  Results from this run will NOT qualify for a leaderboard submission. "
        f"Check out {reference.tag} to run the official release."
    )
    return "\n".join(lines)
