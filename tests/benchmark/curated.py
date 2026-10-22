"""Helpers for the test-only curated 0.2.0 test-split subset."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from nika.workflows.benchmark.release import (
    RESOURCES_V1,
    SCORING,
    TOOLS_V1,
    BenchmarkRelease,
    freeze_release,
    load_release_from_dir,
    write_release_manifest,
)

CURATED_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "curated_0_2_0_test.yaml"
CURATED_VERSION = "curated-0.2.0-test"
RELEASE_TEST_YAML = (
    Path(__file__).resolve().parents[2]
    / "benchmark"
    / "releases"
    / "0.2.0"
    / "test.yaml"
)


def load_curated_cases() -> list[dict[str, Any]]:
    raw = yaml.safe_load(CURATED_FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise ValueError(f"Invalid curated fixture: {CURATED_FIXTURE}")
    return list(raw["cases"])


def case_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Stable identity for subset checks (scenario, problem, size, inject)."""
    return (
        row.get("scenario"),
        row.get("problem"),
        row.get("topo_size"),
        yaml.safe_dump(row.get("inject") or {}, sort_keys=True),
    )


def freeze_curated_release(
    out_dir: Path,
    *,
    n_trials: int = 1,
    version: str = CURATED_VERSION,
) -> BenchmarkRelease:
    """Freeze the curated fixture as a tiny official release for tests."""
    release = freeze_release(
        version=version,
        source_cases=CURATED_FIXTURE,
        out_dir=out_dir,
    )
    defaults = dict(release.defaults)
    defaults["n_trials"] = n_trials
    write_release_manifest(
        out_dir,
        version=version,
        splits=release.splits,
        defaults=defaults,
        scoring=dict(SCORING),
        tools=dict(TOOLS_V1),
        resources=dict(RESOURCES_V1),
        images=release.images,
    )
    return load_release_from_dir(out_dir, split="dev")
