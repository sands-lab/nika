"""Read immutable source evidence at a trajectory's recorded commit."""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
from functools import lru_cache
from pathlib import Path


class SourceUnavailable(ValueError):
    pass


def _git(repo: str, *args: str, allow_no_match: bool = False) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, timeout=20
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise SourceUnavailable("source repository unavailable") from exc
    if result.returncode and not (allow_no_match and result.returncode == 1):
        raise SourceUnavailable("recorded source unavailable")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeError as exc:
        raise SourceUnavailable("source is not UTF-8 text") from exc


@lru_cache(maxsize=256)
def source_files(repo: str, commit: str) -> tuple[str, ...]:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit or ""):
        raise SourceUnavailable("recorded commit is not an immutable hash")
    return tuple(_git(repo, "ls-tree", "-r", "--name-only", commit).splitlines())


@lru_cache(maxsize=512)
def read_source(repo: str, commit: str, path: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit or ""):
        raise SourceUnavailable("recorded commit is not an immutable hash")
    return _git(repo, "show", f"{commit}:{path}")


@lru_cache(maxsize=512)
def source_hash(repo: str, commit: str, path: str) -> str:
    return hashlib.sha256(read_source(repo, commit, path).encode()).hexdigest()


@lru_cache(maxsize=512)
def verified_provider(
    repo: str,
    commit: str,
    fingerprints: tuple[tuple[tuple[str, str], ...], ...],
) -> bool:
    """Require an unchanged provider reviewed at an immutable source commit."""
    source_files(repo, commit)
    for fingerprint in fingerprints:
        try:
            if all(source_hash(repo, commit, p) == h for p, h in fingerprint):
                return True
        except SourceUnavailable:
            continue
    raise SourceUnavailable("provider changed; source review required")


@lru_cache(maxsize=512)
def source_absent(
    repo: str,
    commit: str,
    glob: str,
    fingerprints: tuple[tuple[tuple[str, str], ...], ...],
) -> bool:
    """Check absence only for an unchanged, previously reviewed provider."""
    verified_provider(repo, commit, fingerprints)
    return not any(fnmatch.fnmatchcase(p, glob) for p in source_files(repo, commit))


def request_source(repo: Path, commit: str, request: dict) -> dict:
    """Bounded source operations; audit labels and learned memory are excluded."""
    root = str(repo.resolve())
    path = str(request.get("path", "")).strip("/")
    roots = ("src/agent", "src/nika")
    audit_root = "src/nika/audit"
    if path == audit_root or path.startswith(audit_root + "/"):
        raise SourceUnavailable("audit knowledge is excluded from source evidence")
    root_search = request.get("action") == "search" and path in {"", ".", "src"}
    if request.get("action") == "list" and path in {"", ".", "src"}:
        return {"paths": [p + "/" for p in roots]}
    if not root_search and (
        ".." in Path(path).parts
        or not any(path == p or path.startswith(p + "/") for p in roots)
    ):
        raise SourceUnavailable("use paths under src/agent/ or src/nika/")
    files = [
        p
        for p in source_files(root, commit)
        if (p == path or p.startswith(path + "/"))
        and not p.startswith(audit_root + "/")
    ]
    if request.get("action") == "list":
        return {"paths": files[:80], "total": len(files)}
    if request.get("action") == "read":
        if path not in files:
            raise SourceUnavailable("file absent at the recorded commit")
        lines = read_source(root, commit, path).splitlines()
        start = max(1, int(request.get("start", 1)))
        end = min(len(lines), int(request.get("end", start + 100)), start + 179)
        return {
            "path": path,
            "lines": [f"{i}: {lines[i - 1]}" for i in range(start, end + 1)],
        }
    if request.get("action") != "search" or not str(request.get("query", "")).strip():
        raise SourceUnavailable("request list, read, or a nonempty literal search")
    query = str(request["query"])
    matches = []
    result = _git(
        root,
        "grep",
        "-n",
        "-i",
        "-F",
        "-e",
        query,
        commit,
        "--",
        *(roots if root_search else (path,)),
        allow_no_match=True,
    )
    for hit in result.splitlines():
        parts = hit.split(":", 3)
        if (
            len(parts) == 4
            and parts[2].isdigit()
            and not parts[1].startswith(audit_root + "/")
        ):
            matches.append(
                {"path": parts[1], "line": int(parts[2]), "text": parts[3][:400]}
            )
            if len(matches) == 60:
                break
    return {"matches": matches}
