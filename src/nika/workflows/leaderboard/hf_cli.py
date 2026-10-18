"""Thin Hugging Face Hub helpers for trajectory package PRs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class HuggingFaceCliError(RuntimeError):
    """Failed to talk to the Hugging Face Hub."""


@dataclass(frozen=True)
class HfPullRequestResult:
    repo_id: str
    remote_path: str
    pr_url: str
    pr_num: int | None
    commit_oid: str | None = None


def ensure_hf_token() -> str:
    """Resolve Hub credentials from the environment or saved ``hf auth login``."""
    try:
        from huggingface_hub import get_token
    except ImportError as exc:
        raise HuggingFaceCliError(
            "huggingface_hub is required (install via `uv sync`)."
        ) from exc
    token = get_token()
    if not token:
        raise HuggingFaceCliError(
            "No Hugging Face token found. Run `hf auth login` or set HF_TOKEN "
            "in the environment / repo-root .env."
        )
    return token


def _api() -> Any:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise HuggingFaceCliError(
            "huggingface_hub is required for trajectory submit (install via `uv sync`)."
        ) from exc
    return HfApi(token=ensure_hf_token())


def ensure_hf_auth(repo_id: str) -> None:
    """Check the token and dataset access before creating either remote PR."""
    api = _api()
    try:
        api.whoami()
        api.repo_info(repo_id=repo_id, repo_type="dataset")
    except Exception as exc:  # noqa: BLE001 — Hub raises many types
        raise HuggingFaceCliError(
            f"HF authentication/dataset check failed: {exc}"
        ) from exc


def upload_folder_create_pr(
    *,
    folder_path: Path,
    path_in_repo: str,
    repo_id: str,
    commit_message: str,
    commit_description: str | None = None,
    repo_type: str = "dataset",
) -> HfPullRequestResult:
    """Upload a local folder and open a dataset PR."""
    api = _api()
    try:
        info = api.upload_folder(
            folder_path=str(folder_path),
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type=repo_type,
            commit_message=commit_message,
            commit_description=commit_description,
            create_pr=True,
        )
    except Exception as exc:  # noqa: BLE001 — Hub raises many types
        raise HuggingFaceCliError(f"HF upload/create_pr failed: {exc}") from exc

    pr_url = getattr(info, "pr_url", None) or ""
    pr_num = getattr(info, "pr_num", None)
    if pr_num is not None:
        # Hub PRs created through the API start as drafts; open it for review.
        try:
            api.change_discussion_status(
                repo_id=repo_id,
                discussion_num=int(pr_num),
                new_status="open",
                repo_type=repo_type,
            )
        except Exception as exc:  # noqa: BLE001 — Hub raises many types
            raise HuggingFaceCliError(
                f"HF PR {pr_url or pr_num} was created but is still a draft; "
                f"open it for review on the Hub ({exc})"
            ) from exc
    oid = getattr(info, "oid", None) or getattr(info, "commit_oid", None)
    if not pr_url and pr_num is not None:
        pr_url = f"https://huggingface.co/datasets/{repo_id}/discussions/{pr_num}"
    if not pr_url:
        raise HuggingFaceCliError(
            "HF upload succeeded but no PR URL was returned "
            f"(repo={repo_id}, path={path_in_repo})"
        )
    return HfPullRequestResult(
        repo_id=repo_id,
        remote_path=path_in_repo,
        pr_url=str(pr_url),
        pr_num=int(pr_num) if pr_num is not None else None,
        commit_oid=str(oid) if oid else None,
    )
