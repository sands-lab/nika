"""Scan leaderboard package text for secrets and absolute paths."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "openai_sk",
        re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    ),
    (
        "openai_proj",
        re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}\b"),
    ),
    (
        "anthropic_key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    ),
    (
        "api_key_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|openai_api_key|anthropic_api_key|deepseek_api_key)\s*[=:]\s*\S+"
        ),
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"),
    ),
    (
        "pem_private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
)

_ABS_PATH_RE = re.compile(
    r"(?:^|[\s\"'=])(/home/|/Users/|/tmp/|/var/|/etc/|[A-Za-z]:\\)"
)


def _walk_strings(value: Any, *, path: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path or "$", value))
    elif isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            found.extend(_walk_strings(item, path=child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child = f"{path}[{index}]"
            found.extend(_walk_strings(item, path=child))
    return found


def scan_value_for_issues(value: Any, *, label: str) -> list[str]:
    """Return human-readable findings for secrets or absolute paths in *value*."""
    issues: list[str] = []
    for field_path, text in _walk_strings(value):
        for name, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                issues.append(f"{label}:{field_path}: possible secret ({name})")
                break
        if _ABS_PATH_RE.search(text):
            # Allow URLs with http(s) that may look path-like in rare cases.
            if text.startswith(("http://", "https://")):
                continue
            issues.append(f"{label}:{field_path}: absolute path not allowed")
    return issues


def scan_package_dir(submission_dir: Path) -> list[str]:
    """Scan metadata/README/results JSON+YAML under a submission package."""
    from nika.workflows.leaderboard.schema import (
        FILES_FILENAME,
        IDENTITY_FILENAME,
        METADATA_FILENAME,
        METRICS_FILENAME,
        README_FILENAME,
        RESULTS_DIRNAME,
        TRIAL_RESULT_FILENAME,
        TRIALS_DIRNAME,
    )

    issues: list[str] = []
    targets = [
        submission_dir / METADATA_FILENAME,
        submission_dir / README_FILENAME,
        submission_dir / FILES_FILENAME,
        submission_dir / RESULTS_DIRNAME / IDENTITY_FILENAME,
        submission_dir / RESULTS_DIRNAME / METRICS_FILENAME,
    ]
    trials_root = submission_dir / RESULTS_DIRNAME / TRIALS_DIRNAME
    if trials_root.is_dir():
        targets.extend(sorted(trials_root.glob(f"*/{TRIAL_RESULT_FILENAME}")))

    for path in targets:
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8")
        rel = path.relative_to(submission_dir).as_posix()
        if path.name == README_FILENAME:
            issues.extend(scan_value_for_issues(raw, label=rel))
            continue
        if path.suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
        issues.extend(scan_value_for_issues(data, label=rel))
    return issues
