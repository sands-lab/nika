"""Source, configuration, and container identities for reproducible audits."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from nika.config import REPO_ROOT
from nika.workflows.benchmark.release import read_git_commit


class AuditProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    git_commit: str | None
    git_dirty: bool
    source_sha256: str
    configuration_sha256: str
    started_at: str
    completed_at: str | None = None
    session_id: str
    images: dict[str, dict[str, Any]] = Field(default_factory=dict)


def source_fingerprint() -> str:
    """Hash audit and runtime inputs, including edits in the working tree."""
    paths = (
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                "src",
                "tests/support",
                "experiment",
                "pyproject.toml",
                "uv.lock",
                "config",
                "benchmark/releases",
            ],
            cwd=REPO_ROOT,
        )
        .decode()
        .split("\0")
    )
    digest = hashlib.sha256()
    for relative in sorted(path for path in paths if path):
        path = REPO_ROOT / relative
        digest.update(relative.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<deleted>")
        digest.update(b"\0")
    return digest.hexdigest()


def configuration_fingerprint() -> str:
    from nika.run_config.loader import get_run_config

    config = get_run_config().nika.model_dump(mode="json", exclude={"result_dir"})
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


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
        source_sha256=source_fingerprint(),
        configuration_sha256=configuration_fingerprint(),
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
