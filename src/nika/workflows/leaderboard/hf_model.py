"""Pin the Hugging Face model and revision an open-weight submission ran.

The leaderboard prices open weights and dates their release from this repo, so
``submit`` records it in ``agent.extra`` (``hf_repo``, ``hf_revision``,
``hf_revision_source``). Runs against a vendor API (any provider other than
``custom``) need no repo.
"""

from __future__ import annotations

import re
from typing import Any

HF_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class HfModelError(ValueError):
    """The submitted model's Hugging Face repo cannot be determined."""


def _model_info(repo: str, revision: str | None) -> Any:
    try:
        from huggingface_hub import HfApi
        from huggingface_hub.errors import (
            RepositoryNotFoundError,
            RevisionNotFoundError,
        )
    except ImportError as exc:  # pragma: no cover - installed by uv sync
        raise HfModelError(
            "huggingface_hub is required (install via `uv sync`)."
        ) from exc
    try:
        return HfApi().model_info(repo, revision=revision)
    except RevisionNotFoundError as exc:
        raise HfModelError(
            f"Revision {revision!r} not found in Hugging Face model {repo!r}."
        ) from exc
    except RepositoryNotFoundError:
        return None
    except Exception as exc:
        raise HfModelError(f"Could not query Hugging Face for {repo!r}: {exc}") from exc


def _missing_repo_error(model: str) -> HfModelError:
    return HfModelError(
        f'Model "{model}" in run.json is not a Hugging Face model ID.\n'
        "  Open-weight model: pass --hf-model <org/repo> "
        "[--hf-revision <commit|branch|tag>] (default: latest revision).\n"
        "  Proprietary model behind a custom endpoint: pass --proprietary."
    )


def resolve_hf_model(
    run_cfg: dict[str, Any],
    extra: dict[str, Any],
    *,
    hf_model: str | None = None,
    hf_revision: str | None = None,
    proprietary: bool = False,
) -> dict[str, str]:
    """Return ``agent.extra`` fields that pin the run's Hugging Face model.

    The repo comes from ``--hf-model``, else ``agent.extra.hf_repo``, else the
    run's ``model`` when a custom endpoint served a Hugging Face ID (vLLM's
    default). The revision comes from ``--hf-revision``, else
    ``agent.extra.hf_revision``, else the repo's latest revision at submit time.
    Either way it is stored as a commit SHA. Returns ``{}`` for vendor API runs.
    """
    if proprietary:
        if hf_model or hf_revision:
            raise HfModelError(
                "--proprietary cannot be combined with --hf-model/--hf-revision."
            )
        return {}
    if hf_revision and not (hf_model or extra.get("hf_repo")):
        raise HfModelError("--hf-revision requires --hf-model.")

    model = str(run_cfg.get("model") or "").strip()
    provider = str(run_cfg.get("llm_provider") or "").strip().lower()
    repo = (hf_model or str(extra.get("hf_repo") or "")).strip()
    detected = not repo
    if detected:
        # Vendor APIs need no repo; runs without a recorded provider predate it.
        if provider != "custom":
            return {}
        if not HF_REPO_RE.match(model):
            raise _missing_repo_error(model)
        repo = model
    elif not HF_REPO_RE.match(repo):
        raise HfModelError(f"--hf-model must look like org/repo, got {repo!r}.")

    if not hf_revision and not hf_model:
        hf_revision = str(extra.get("hf_revision") or "").strip() or None
    revision = (hf_revision or "").strip() or None
    info = _model_info(repo, revision)
    if info is None:
        if detected:
            raise _missing_repo_error(model)
        raise HfModelError(f"Hugging Face model {repo!r} not found.")
    source = "specified" if revision else "latest"
    return {"hf_repo": info.id, "hf_revision": info.sha, "hf_revision_source": source}
