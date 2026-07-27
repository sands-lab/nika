"""Submission metadata.yaml + README.md templates and loaders."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from nika.workflows.leaderboard.schema import (
    METADATA_FILENAME,
    README_FILENAME,
    SubmissionMetadata,
)

DEFAULT_SUBMISSION_DIRNAME = "submission"

META_TEMPLATE: dict[str, Any] = {
    "info": {
        "name": "",
        "authors": "",
        "org": None,
        "site": None,
        "report": None,
        "logo": None,
        "email": None,
        "github": None,
    },
    "agent": {
        "model": "",
        "framework": "",
        "tools": [],
        "skills": [],
        "optimization_methods": [],
        "tags": [],
        "os_model": False,
        "os_system": False,
        "extra": {},
    },
}

README_TEMPLATE = """# <System name>

Brief description of your troubleshooting agent / system.

## Links

- Code / library: <URL>
- Paper / report / blog: <URL>
- Project site: <URL>

## Authors

- <Name> (<affiliation>, optional profile URL)

## Citation

```
@misc{...,
  title={...},
  author={...},
  year={...},
  url={...}
}
```
"""


class MetaInputError(ValueError):
    """Invalid submission metadata / template input."""


def default_meta_template() -> dict[str, Any]:
    """Return a deep copy of the empty metadata.yaml template."""
    return yaml.safe_load(yaml.safe_dump(META_TEMPLATE))


def write_submission_templates(path: str | Path) -> Path:
    """Write ``metadata.yaml`` + ``README.md`` templates under ``path``."""
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / METADATA_FILENAME
    readme_path = out / README_FILENAME
    meta_path.write_text(
        yaml.safe_dump(default_meta_template(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    readme_path.write_text(README_TEMPLATE, encoding="utf-8")
    return out.resolve()


def write_meta_template(path: str | Path) -> Path:
    """Compatibility alias: write submission templates under ``path`` (a directory)."""
    return write_submission_templates(path)


def _normalize_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_list_field(value: Any, *, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise MetaInputError(f"{field} entries must be non-empty strings")
            out.append(item.strip())
        return out
    raise MetaInputError(f"{field} must be a list of strings")


def parse_metadata_payload(data: dict[str, Any]) -> SubmissionMetadata:
    """Validate a ``{info, agent}`` mapping into ``SubmissionMetadata``."""
    info_raw = data.get("info")
    agent_raw = data.get("agent")
    if not isinstance(info_raw, dict):
        raise MetaInputError("metadata.info must be an object")
    if not isinstance(agent_raw, dict):
        raise MetaInputError("metadata.agent must be an object")

    info_data = {
        "name": str(info_raw.get("name") or "").strip(),
        "authors": str(info_raw.get("authors") or "").strip(),
        "org": _normalize_optional_str(info_raw.get("org")),
        "site": _normalize_optional_str(info_raw.get("site")),
        "report": _normalize_optional_str(info_raw.get("report")),
        "logo": _normalize_optional_str(info_raw.get("logo")),
        "email": _normalize_optional_str(info_raw.get("email")),
        "github": _normalize_optional_str(info_raw.get("github")),
    }
    agent_data = {
        "model": str(agent_raw.get("model") or "").strip(),
        "framework": str(agent_raw.get("framework") or "").strip(),
        "tools": _parse_list_field(agent_raw.get("tools"), field="tools"),
        "skills": _parse_list_field(agent_raw.get("skills"), field="skills"),
        "optimization_methods": _parse_list_field(
            agent_raw.get("optimization_methods"), field="optimization_methods"
        ),
        "tags": _parse_list_field(agent_raw.get("tags"), field="tags"),
        "os_model": bool(agent_raw.get("os_model", False)),
        "os_system": bool(agent_raw.get("os_system", False)),
        "extra": agent_raw.get("extra")
        if isinstance(agent_raw.get("extra"), dict)
        else {},
    }
    try:
        return SubmissionMetadata.model_validate(
            {"info": info_data, "agent": agent_data}
        )
    except Exception as exc:
        raise MetaInputError(f"invalid metadata: {exc}") from exc


def load_metadata_file(path: str | Path) -> SubmissionMetadata:
    """Load and validate ``metadata.yaml``."""
    file_path = Path(path)
    if not file_path.is_file():
        raise MetaInputError(f"metadata file not found: {file_path}")
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MetaInputError(f"failed to parse metadata {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MetaInputError(f"metadata must be a YAML object: {file_path}")
    return parse_metadata_payload(data)


def load_submission_dir(path: str | Path) -> tuple[SubmissionMetadata, Path, Path]:
    """Load ``metadata.yaml`` + ``README.md`` from a submission staging directory.

    Returns ``(metadata, metadata_path, readme_path)``.
    """
    root = Path(path)
    if not root.is_dir():
        raise MetaInputError(f"submission directory not found: {root}")
    meta_path = root / METADATA_FILENAME
    readme_path = root / README_FILENAME
    if not meta_path.is_file():
        raise MetaInputError(f"missing {METADATA_FILENAME} under {root}")
    if not readme_path.is_file():
        raise MetaInputError(f"missing {README_FILENAME} under {root}")
    metadata = load_metadata_file(meta_path)
    return metadata, meta_path, readme_path


def slugify_name(name: str) -> str:
    """Sanitize ``info.name`` into a folder slug (lowercase, ``_`` separators)."""
    text = name.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        raise MetaInputError("info.name must yield a non-empty slug")
    return text
