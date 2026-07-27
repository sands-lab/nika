"""Thin wrappers around ``gh`` and ``git`` for leaderboard submit."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class GitHubCliError(RuntimeError):
    """Raised when a ``gh`` or ``git`` invocation fails."""


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


def _which(name: str) -> str | None:
    return shutil.which(name)


def run_command(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    check: bool = True,
    input_text: str | None = None,
) -> CommandResult:
    """Run a subprocess and optionally raise ``GitHubCliError`` on failure."""
    try:
        completed = subprocess.run(
            list(args),
            cwd=str(cwd) if cwd is not None else None,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise GitHubCliError(f"failed to run {args[0]!r}: {exc}") from exc

    result = CommandResult(
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
        returncode=completed.returncode,
    )
    if check and result.returncode != 0:
        detail = result.stderr or result.stdout or f"exit {result.returncode}"
        raise GitHubCliError(f"{' '.join(args)}: {detail}")
    return result


def require_gh() -> str:
    path = _which("gh")
    if not path:
        raise GitHubCliError(
            "GitHub CLI (`gh`) not found on PATH. Install it from "
            "https://cli.github.com/ then run `gh auth login`."
        )
    return path


def require_git() -> str:
    path = _which("git")
    if not path:
        raise GitHubCliError("`git` not found on PATH.")
    return path


def ensure_gh_auth() -> None:
    """Fail fast unless ``gh`` is authenticated."""
    gh = require_gh()
    result = run_command([gh, "auth", "status"], check=False)
    if result.returncode != 0:
        raise GitHubCliError(
            "GitHub CLI is not authenticated. Run `gh auth login` "
            "or set GH_TOKEN, then retry."
        )


def gh_git_protocol() -> str:
    """Return configured git protocol (`https` or `ssh`).

    Prefer the active account's "Git operations protocol" from ``gh auth status``
    (what ``gh`` itself uses). Fall back to ``gh config get git_protocol``.
    """
    gh = require_gh()
    status = run_command([gh, "auth", "status"], check=False)
    blob = f"{status.stdout}\n{status.stderr}".lower()
    if "git operations protocol: ssh" in blob:
        return "ssh"
    if "git operations protocol: https" in blob:
        return "https"
    result = run_command([gh, "config", "get", "git_protocol"], check=False)
    proto = (result.stdout or "").strip().lower()
    return proto if proto in {"https", "ssh"} else "https"


def clone_url_for_repo(repo: str, *, protocol: str | None = None) -> str:
    owner_repo = repo.strip().removesuffix(".git")
    if owner_repo.count("/") != 1:
        raise GitHubCliError(f"expected owner/name repo id, got {repo!r}")
    proto = protocol or gh_git_protocol()
    if proto == "ssh":
        return f"git@github.com:{owner_repo}.git"
    return f"https://github.com/{owner_repo}.git"


def repo_permissions(repo: str) -> dict[str, bool]:
    """Return permission flags for the authenticated user on ``repo``."""
    gh = require_gh()
    result = run_command(
        [gh, "api", f"repos/{repo}", "--jq", ".permissions"],
    )
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise GitHubCliError(f"invalid permissions payload for {repo}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GitHubCliError(f"unexpected permissions payload for {repo}: {payload!r}")
    return {
        "admin": bool(payload.get("admin")),
        "maintain": bool(payload.get("maintain")),
        "push": bool(payload.get("push")),
        "triage": bool(payload.get("triage")),
        "pull": bool(payload.get("pull", True)),
    }


def can_push(repo: str) -> bool:
    perms = repo_permissions(repo)
    return bool(perms.get("push") or perms.get("admin") or perms.get("maintain"))


def current_login() -> str:
    gh = require_gh()
    result = run_command([gh, "api", "user", "--jq", ".login"])
    login = (result.stdout or "").strip()
    if not login:
        raise GitHubCliError("could not resolve authenticated GitHub login")
    return login


def ensure_fork(repo: str) -> str:
    """Ensure a fork of ``repo`` exists; return ``owner/name`` of the fork."""
    gh = require_gh()
    login = current_login()
    owner, name = repo.split("/", 1)
    fork_repo = f"{login}/{name}"
    # Already exists?
    probe = run_command(
        [gh, "api", f"repos/{fork_repo}", "--jq", ".full_name"], check=False
    )
    if probe.returncode == 0 and (probe.stdout or "").strip():
        return (probe.stdout or "").strip()
    run_command(
        [gh, "repo", "fork", repo, "--default-branch-only", "--fork-name", name],
    )
    # Resolve again (org forks / rename edge cases).
    probe = run_command(
        [gh, "api", f"repos/{fork_repo}", "--jq", ".full_name"], check=False
    )
    if probe.returncode == 0 and (probe.stdout or "").strip():
        return (probe.stdout or "").strip()
    # Fallback: list forks created by the user pointing at upstream.
    listed = run_command(
        [
            gh,
            "api",
            f"repos/{owner}/{name}/forks?per_page=100",
            "--jq",
            f'[.[] | select(.owner.login=="{login}")][0].full_name',
        ],
        check=False,
    )
    fork_name = (listed.stdout or "").strip()
    if fork_name and fork_name != "null":
        return fork_name
    raise GitHubCliError(f"fork of {repo} was created but could not be resolved")


def git_clone(url: str, dest: Path, *, depth: int | None = 1) -> None:
    git = require_git()
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [git, "clone"]
    if depth is not None:
        args.extend(["--depth", str(depth)])
    args.extend([url, str(dest)])
    run_command(args)


def git_checkout_new_branch(repo_dir: Path, branch: str) -> None:
    git = require_git()
    run_command([git, "checkout", "-B", branch], cwd=repo_dir)


def git_add_all(repo_dir: Path) -> None:
    git = require_git()
    run_command([git, "add", "-A"], cwd=repo_dir)


def git_commit(repo_dir: Path, message: str) -> bool:
    """Create a commit if there are staged/index changes. Returns whether a commit was made."""
    git = require_git()
    status = run_command([git, "status", "--porcelain"], cwd=repo_dir)
    if not status.stdout:
        return False
    run_command([git, "add", "-A"], cwd=repo_dir)
    run_command(
        [git, "commit", "-m", message],
        cwd=repo_dir,
    )
    return True


def git_push(repo_dir: Path, *, remote: str = "origin", branch: str) -> None:
    git = require_git()
    run_command([git, "push", "-u", remote, branch], cwd=repo_dir)


def git_delete_remote_branch(
    repo_dir: Path, *, remote: str = "origin", branch: str
) -> None:
    git = require_git()
    run_command([git, "push", remote, "--delete", branch], cwd=repo_dir, check=False)


def create_pull_request(
    *,
    repo: str,
    head: str,
    base: str = "main",
    title: str,
    body: str,
    draft: bool = False,
) -> str:
    """Open a PR and return its URL."""
    gh = require_gh()
    args = [
        gh,
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        base,
        "--head",
        head,
        "--title",
        title,
        "--body",
        body,
    ]
    if draft:
        args.append("--draft")
    result = run_command(args)
    url = (result.stdout or "").strip().splitlines()[-1].strip()
    if not url.startswith("http"):
        raise GitHubCliError(f"gh pr create did not return a URL: {result.stdout!r}")
    return url


def close_pull_request(repo: str, number: int, *, comment: str | None = None) -> None:
    gh = require_gh()
    if comment:
        run_command(
            [gh, "pr", "comment", str(number), "--repo", repo, "--body", comment],
            check=False,
        )
    run_command([gh, "pr", "close", str(number), "--repo", repo, "--delete-branch"])


def pr_number_from_url(url: str) -> int:
    # https://github.com/owner/repo/pull/123
    parts = url.rstrip("/").split("/")
    if len(parts) < 2 or parts[-2] != "pull":
        raise GitHubCliError(f"cannot parse PR number from URL: {url!r}")
    try:
        return int(parts[-1])
    except ValueError as exc:
        raise GitHubCliError(f"cannot parse PR number from URL: {url!r}") from exc
