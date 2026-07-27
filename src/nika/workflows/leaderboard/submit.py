"""Open a GitHub PR for a packed leaderboard submission package."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from nika.config import REPO_ROOT
from nika.workflows.leaderboard import github_cli as gh
from nika.workflows.leaderboard.remote import (
    DEFAULT_LEADERBOARD_REPO,
    remote_submission_relpath,
    submission_branch_name,
)
from nika.workflows.leaderboard.schema import (
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    RESULTS_DIRNAME,
)
from nika.workflows.leaderboard.validate import (
    LeaderboardValidateError,
    validate_leaderboard_submission,
)


class LeaderboardSubmitError(RuntimeError):
    """Failed to submit a leaderboard package to GitHub."""


@dataclass(frozen=True)
class SubmitResult:
    repo: str
    package_dir: Path
    remote_path: str
    branch: str
    head: str
    pr_url: str
    used_fork: bool


def _resolve_package_dir(path: str | Path) -> Path:
    root = Path(path)
    if not root.is_absolute():
        root = (REPO_ROOT / root).resolve()
    else:
        root = root.resolve()
    return root


def _load_release_version(package_dir: Path) -> str:
    identity_path = package_dir / RESULTS_DIRNAME / IDENTITY_FILENAME
    if not identity_path.is_file():
        raise LeaderboardSubmitError(
            f"missing {RESULTS_DIRNAME}/{IDENTITY_FILENAME} in {package_dir}"
        )
    try:
        payload = yaml.safe_load(identity_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LeaderboardSubmitError(f"invalid identity.yaml: {exc}") from exc
    if not isinstance(payload, dict):
        raise LeaderboardSubmitError("identity.yaml must be a YAML object")
    benchmark = payload.get("benchmark")
    if not isinstance(benchmark, dict):
        raise LeaderboardSubmitError("identity.yaml missing benchmark mapping")
    version = benchmark.get("version")
    if not isinstance(version, str) or not version.strip():
        raise LeaderboardSubmitError("identity.yaml missing benchmark.version")
    return version.strip()


def _default_pr_body(package_dir: Path, remote_path: str) -> str:
    meta_path = package_dir / METADATA_FILENAME
    name = package_dir.name
    if meta_path.is_file():
        try:
            raw = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                info = raw.get("info")
                if isinstance(info, dict) and info.get("name"):
                    name = str(info["name"])
        except yaml.YAMLError:
            pass
    return (
        f"## Leaderboard submission\n\n"
        f"- Entry: **{name}**\n"
        f"- Package path: `{remote_path}`\n\n"
        f"Opened by `nika leaderboard submit`.\n\n"
        f"Please follow the PR checklist in "
        f"https://github.com/sands-lab/nika/blob/main/docs/leaderboard-submission.md\n"
    )


def _copy_package(package_dir: Path, dest: Path) -> None:
    if dest.exists():
        raise LeaderboardSubmitError(
            f"remote path already exists in clone: {dest.as_posix()}. "
            "Choose a different package name/date or update the existing submission."
        )
    shutil.copytree(package_dir, dest)


def submit_leaderboard_package(
    package_dir: str | Path,
    *,
    repo: str = DEFAULT_LEADERBOARD_REPO,
    draft: bool = False,
    skip_validate: bool = False,
    title: str | None = None,
    body: str | None = None,
    base: str = "main",
    work_dir: Path | str | None = None,
) -> SubmitResult:
    """Validate (optional), push a submission branch, and open a PR.

    Users with push access push the branch directly to ``repo``. Others fork
    first and open a PR from the fork.
    """
    root = _resolve_package_dir(package_dir)
    if not root.is_dir():
        raise LeaderboardSubmitError(f"package directory not found: {root}")

    if not skip_validate:
        report = validate_leaderboard_submission(root)
        if not report.ok:
            raise LeaderboardValidateError("; ".join(report.errors))

    try:
        gh.ensure_gh_auth()
    except gh.GitHubCliError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc

    release_version = _load_release_version(root)
    package_name = root.name
    try:
        remote_rel = remote_submission_relpath(release_version, package_name)
        branch = submission_branch_name(package_name)
    except ValueError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc

    try:
        push_ok = gh.can_push(repo)
    except gh.GitHubCliError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc

    used_fork = False
    push_repo = repo
    if not push_ok:
        try:
            push_repo = gh.ensure_fork(repo)
            used_fork = True
        except gh.GitHubCliError as exc:
            raise LeaderboardSubmitError(str(exc)) from exc

    tmp_owned = work_dir is None
    parent = (
        Path(work_dir)
        if work_dir is not None
        else Path(tempfile.mkdtemp(prefix="nika-lb-submit-"))
    )
    clone_dest = parent / "repo" if tmp_owned else parent

    try:
        url = gh.clone_url_for_repo(push_repo)
        gh.git_clone(url, clone_dest, depth=1)
        gh.git_checkout_new_branch(clone_dest, branch)

        dest = clone_dest / remote_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy_package(root, dest)

        gh.git_add_all(clone_dest)
        committed = gh.git_commit(
            clone_dest,
            f"Add leaderboard submission {remote_rel}",
        )
        if not committed:
            raise LeaderboardSubmitError(
                "nothing to commit after copying package "
                "(identical content already present?)"
            )
        gh.git_push(clone_dest, branch=branch)

        login = gh.current_login()
        head = branch if push_repo == repo else f"{login}:{branch}"
        pr_title = title or f"Add submission {package_name}"
        pr_body = body or _default_pr_body(root, remote_rel)
        pr_url = gh.create_pull_request(
            repo=repo,
            head=head,
            base=base,
            title=pr_title,
            body=pr_body,
            draft=draft,
        )
        return SubmitResult(
            repo=repo,
            package_dir=root,
            remote_path=remote_rel,
            branch=branch,
            head=head,
            pr_url=pr_url,
            used_fork=used_fork,
        )
    except gh.GitHubCliError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc
    finally:
        if tmp_owned and parent.exists():
            shutil.rmtree(parent, ignore_errors=True)
