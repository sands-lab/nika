"""Pack, validate, and open GitHub / Hugging Face PRs for a release run."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from nika.config import REPO_ROOT
from nika.workflows.leaderboard import github_cli as gh
from nika.workflows.leaderboard import hf_cli as hf
from nika.workflows.leaderboard.hf_remote import (
    DEFAULT_TRAJECTORIES_REPO,
    remote_trajectories_relpath,
)
from nika.workflows.leaderboard.meta_input import MetaInputError
from nika.workflows.leaderboard.pack import (
    LeaderboardPackError,
    PackResult,
    pack_leaderboard_submission,
)
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
from nika.workflows.leaderboard.validate_trajectories import (
    sibling_trajectories_dir,
    validate_trajectory_package,
)


class LeaderboardSubmitError(RuntimeError):
    """Failed to submit a leaderboard package to GitHub and/or Hugging Face."""


@dataclass(frozen=True)
class SubmitResult:
    repo: str
    package_dir: Path
    remote_path: str | None
    branch: str | None
    head: str | None
    pr_url: str | None
    used_fork: bool
    trajectories_repo: str | None = None
    trajectories_dir: Path | None = None
    trajectories_remote_path: str | None = None
    trajectories_pr_url: str | None = None
    trajectories_pr_num: int | None = None


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
        # Trajectory package keeps identity.yaml at the root.
        identity_path = package_dir / IDENTITY_FILENAME
    if not identity_path.is_file():
        raise LeaderboardSubmitError(
            f"missing identity.yaml in {package_dir}"
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


def _default_pr_body(
    package_dir: Path,
    remote_path: str,
    *,
    trajectories_remote_path: str | None = None,
) -> str:
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
    traj_line = ""
    if trajectories_remote_path:
        traj_line = f"- Trajectories path (HF): `{trajectories_remote_path}`\n"
    return (
        f"## Leaderboard submission\n\n"
        f"- Entry: **{name}**\n"
        f"- Package path: `{remote_path}`\n"
        f"{traj_line}\n"
        f"Opened by `nika leaderboard submit`.\n\n"
        f"Please follow the PR checklist in "
        f"https://github.com/sands-lab/nika/blob/main/docs/benchmarks/leaderboard-submission.md\n"
    )


def _default_hf_pr_description(
    package_dir: Path,
    remote_path: str,
    *,
    scores_remote_path: str | None = None,
) -> str:
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
    scores_line = (
        f"- Scores package (GitHub): `{scores_remote_path}`\n"
        if scores_remote_path
        else ""
    )
    return (
        f"## NIKA trajectories submission\n\n"
        f"- Entry: **{name}**\n"
        f"- Path: `{remote_path}`\n"
        f"{scores_line}\n"
        f"Opened by `nika leaderboard submit`.\n"
    )


def _copy_package(package_dir: Path, dest: Path) -> None:
    if dest.exists():
        raise LeaderboardSubmitError(
            f"remote path already exists in clone: {dest.as_posix()}. "
            "Choose a different package name/date or update the existing submission."
        )
    shutil.copytree(package_dir, dest)


def _submit_github(
    root: Path,
    *,
    repo: str,
    draft: bool,
    title: str | None,
    body: str | None,
    base: str,
    work_dir: Path | str | None,
    trajectories_remote_path: str | None,
) -> SubmitResult:
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
        pr_body = body or _default_pr_body(
            root,
            remote_rel,
            trajectories_remote_path=trajectories_remote_path,
        )
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
            trajectories_remote_path=trajectories_remote_path,
        )
    except gh.GitHubCliError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc
    finally:
        if tmp_owned and parent.exists():
            shutil.rmtree(parent, ignore_errors=True)


def _submit_trajectories(
    traj_root: Path,
    *,
    scores_root: Path,
    traj_repo: str,
) -> tuple[str, str, int | None]:
    try:
        hf.ensure_hf_token()
    except hf.HuggingFaceCliError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc

    release_version = _load_release_version(scores_root)
    package_name = scores_root.name
    try:
        remote_rel = remote_trajectories_relpath(release_version, package_name)
    except ValueError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc

    scores_remote = None
    try:
        scores_remote = remote_submission_relpath(release_version, package_name)
    except ValueError:
        scores_remote = None

    try:
        result = hf.upload_folder_create_pr(
            folder_path=traj_root,
            path_in_repo=remote_rel,
            repo_id=traj_repo,
            commit_message=f"Add trajectories {remote_rel}",
            commit_description=_default_hf_pr_description(
                traj_root,
                remote_rel,
                scores_remote_path=scores_remote,
            ),
        )
    except hf.HuggingFaceCliError as exc:
        raise LeaderboardSubmitError(str(exc)) from exc

    return remote_rel, result.pr_url, result.pr_num


def _submit_packed_package(
    package_dir: str | Path,
    *,
    repo: str = DEFAULT_LEADERBOARD_REPO,
    draft: bool = False,
    skip_validate: bool = False,
    title: str | None = None,
    body: str | None = None,
    base: str = "main",
    work_dir: Path | str | None = None,
    skip_github: bool = False,
    skip_trajectories: bool = False,
    traj_repo: str = DEFAULT_TRAJECTORIES_REPO,
    trajectories_dir: str | Path | None = None,
) -> SubmitResult:
    """Validate (optional) a packed package, then open GitHub and/or HF PRs."""
    if skip_github and skip_trajectories:
        raise LeaderboardSubmitError(
            "nothing to submit: both --skip-github and --skip-trajectories set"
        )

    root = _resolve_package_dir(package_dir)
    if not root.is_dir():
        raise LeaderboardSubmitError(f"package directory not found: {root}")

    traj_root: Path | None = None
    if not skip_trajectories:
        traj_root = (
            _resolve_package_dir(trajectories_dir)
            if trajectories_dir is not None
            else sibling_trajectories_dir(root)
        )
        if not traj_root.is_dir():
            raise LeaderboardSubmitError(
                f"trajectories package not found: {traj_root} "
                "(pack did not produce a sibling _trajectories/ directory; "
                "pass --skip-trajectories to submit scores only)"
            )

    if not skip_validate:
        report = validate_leaderboard_submission(root)
        if not report.ok:
            raise LeaderboardValidateError("; ".join(report.errors))
        if traj_root is not None:
            traj_report = validate_trajectory_package(traj_root, scores_dir=root)
            if not traj_report.ok:
                raise LeaderboardValidateError("; ".join(traj_report.errors))

    traj_remote: str | None = None
    traj_pr_url: str | None = None
    traj_pr_num: int | None = None
    if traj_root is not None:
        release_version = _load_release_version(root)
        try:
            traj_remote = remote_trajectories_relpath(release_version, root.name)
        except ValueError as exc:
            raise LeaderboardSubmitError(str(exc)) from exc

    gh_result: SubmitResult | None = None
    if not skip_github:
        gh_result = _submit_github(
            root,
            repo=repo,
            draft=draft,
            title=title,
            body=body,
            base=base,
            work_dir=work_dir,
            trajectories_remote_path=traj_remote,
        )

    if traj_root is not None:
        traj_remote, traj_pr_url, traj_pr_num = _submit_trajectories(
            traj_root,
            scores_root=root,
            traj_repo=traj_repo,
        )

    if gh_result is not None:
        return SubmitResult(
            repo=gh_result.repo,
            package_dir=gh_result.package_dir,
            remote_path=gh_result.remote_path,
            branch=gh_result.branch,
            head=gh_result.head,
            pr_url=gh_result.pr_url,
            used_fork=gh_result.used_fork,
            trajectories_repo=traj_repo if traj_root is not None else None,
            trajectories_dir=traj_root,
            trajectories_remote_path=traj_remote,
            trajectories_pr_url=traj_pr_url,
            trajectories_pr_num=traj_pr_num,
        )

    return SubmitResult(
        repo=repo,
        package_dir=root,
        remote_path=None,
        branch=None,
        head=None,
        pr_url=None,
        used_fork=False,
        trajectories_repo=traj_repo,
        trajectories_dir=traj_root,
        trajectories_remote_path=traj_remote,
        trajectories_pr_url=traj_pr_url,
        trajectories_pr_num=traj_pr_num,
    )


def submit_leaderboard_package(
    result_dir: str | Path,
    *,
    submission_dir: str | Path,
    out_dir: str | Path | None = None,
    repo: str = DEFAULT_LEADERBOARD_REPO,
    draft: bool = False,
    skip_validate: bool = False,
    title: str | None = None,
    body: str | None = None,
    base: str = "main",
    work_dir: Path | str | None = None,
    skip_github: bool = False,
    skip_trajectories: bool = False,
    traj_repo: str = DEFAULT_TRAJECTORIES_REPO,
) -> SubmitResult:
    """Pack a release run, validate packages, then open GitHub and/or HF PRs.

    Pack or validate failures raise before any remote submit. Default: both
    GitHub scores and HF trajectories PRs. Use ``skip_github`` /
    ``skip_trajectories`` to disable one side.
    """
    try:
        packed: PackResult = pack_leaderboard_submission(
            result_dir,
            submission_dir=submission_dir,
            out_dir=out_dir,
        )
    except (LeaderboardPackError, MetaInputError) as exc:
        raise LeaderboardSubmitError(f"pack failed: {exc}") from exc

    return _submit_packed_package(
        packed.scores_dir,
        repo=repo,
        draft=draft,
        skip_validate=skip_validate,
        title=title,
        body=body,
        base=base,
        work_dir=work_dir,
        skip_github=skip_github,
        skip_trajectories=skip_trajectories,
        traj_repo=traj_repo,
        trajectories_dir=packed.trajectories_dir,
    )
