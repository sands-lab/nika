"""Source, configuration, and container identities for reproducible audits."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nika.workflows.benchmark.release import read_git_commit


class AuditProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git_commit: str | None
    git_dirty: bool
    started_at: str
    completed_at: str | None = None
    session_id: str
    images: dict[str, dict[str, Any]] = Field(default_factory=dict)


def capture_provenance(session_id: str, runtime: Any) -> AuditProvenance:
    commit, dirty = read_git_commit()
    images: dict[str, dict[str, Any]] = {}
    for node in runtime.list_nodes():
        container = runtime.get_container(node)
        image = container.image
        images[node] = {
            "reference": container.attrs["Config"]["Image"],
            "image_id": image.id,
            "repo_digests": image.attrs.get("RepoDigests") or [],
        }
    return AuditProvenance(
        git_commit=commit,
        git_dirty=dirty,
        started_at=datetime.now(UTC).isoformat(),
        session_id=session_id,
        images=images,
    )


def provenance_complete(provenance: AuditProvenance | None) -> bool:
    """Reject records without a finished, attributable live run."""
    return bool(
        provenance is not None
        and provenance.git_commit
        and provenance.completed_at
        and provenance.images
    )


def provenance_current(provenance: AuditProvenance | None) -> bool:
    """Accept completed evidence from the current clean commit."""
    commit, dirty = read_git_commit()
    return bool(
        provenance_complete(provenance)
        and provenance is not None
        and provenance.git_commit == commit
        and not provenance.git_dirty
        and not dirty
    )
