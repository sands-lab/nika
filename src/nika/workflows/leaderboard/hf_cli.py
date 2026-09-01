"""Thin Hugging Face Hub helpers for trajectory package PRs."""

from __future__ import annotations

import os
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


def resolve_hf_token() -> str | None:
    """Return a Hub write token from ``HF_TOKEN``."""
    value = os.environ.get("HF_TOKEN", "").strip()
    return value or None


def ensure_hf_token() -> str:
    token = resolve_hf_token()
    if not token:
        raise HuggingFaceCliError(
            "No Hugging Face token found. Set HF_TOKEN in the environment / "
            "repo-root .env (write access to the trajectories dataset required)."
        )
    return token


def _api(token: str | None = None) -> Any:
    try:
        from huggingface_hub import HfApi
    except ImportError as exc:
        raise HuggingFaceCliError(
            "huggingface_hub is required for trajectory submit "
            "(install via `uv sync`)."
        ) from exc
    return HfApi(token=token or ensure_hf_token())


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
    oid = getattr(info, "oid", None) or getattr(info, "commit_oid", None)
    if not pr_url and pr_num is not None:
        pr_url = (
            f"https://huggingface.co/datasets/{repo_id}/discussions/{pr_num}"
        )
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
