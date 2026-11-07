"""Shared helpers for benchmark trial tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

ROW_A = {
    "scenario": "dc_clos",
    "problem": "link_down",
    "topo_size": "s",
    "inject": {"host_name": "client_0", "intf_name": "eth0"},
}
ROW_B = {
    "scenario": "dc_clos",
    "problem": "link_flap",
    "topo_size": "s",
    "inject": {"host_name": "client_0", "intf_name": "eth0"},
}


def write_valid_trial(
    path: Path,
    *,
    outcome: str = "success",
    session_id: str | None = None,
    fingerprint: str | None = None,
) -> None:
    path.mkdir(parents=True, exist_ok=True)
    sid = session_id or path.name
    run_meta = {
        "session_id": sid,
        "status": "finished",
        "outcome": outcome,
        "benchmark_fingerprint": fingerprint or "fp",
    }
    (path / "run.json").write_text(json.dumps(run_meta), encoding="utf-8")
    (path / "ground_truth.json").write_text("{}", encoding="utf-8")
    (path / "messages.jsonl").write_text("", encoding="utf-8")
    (path / "eval_metrics.json").write_text("{}", encoding="utf-8")
    if outcome == "success":
        (path / "submission.json").write_text("{}", encoding="utf-8")


def write_agent_started(path: Path) -> None:
    """Session evidence that the agent got a turn (so failures are counted)."""
    path.mkdir(parents=True, exist_ok=True)
    ts = "2026-09-23T22:00:00+00:00"
    with (path / "nika.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": ts, "event": "agent_start"}) + "\n")
    with (path / "messages.jsonl").open("a", encoding="utf-8") as handle:
        for event in ("llm_start", "llm_end"):
            handle.write(json.dumps({"timestamp": ts, "event": event}) + "\n")


def mini_cases_yaml(path: Path, rows: list[dict] | None = None) -> Path:
    payload = {"seed": 42, "cases": rows or [ROW_A, ROW_B]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@contextmanager
def pinned_engine_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    release_ref: str = "0.2.0",
    pinned: bool = False,
) -> Iterator[tuple[str, str]]:
    """Copy a release with its pinned engine or one built from committed ``HEAD``.

    Yields the engine tag and image. An image built for HEAD is removed after
    the test; supplied and release-pinned images are retained for reuse.
    """
    from nika.benchmark_engine.container import (
        _git_commit,
        image_id,
        resolve_engine_image,
    )
    from nika.workflows.benchmark.release import RELEASES_DIR, load_release

    release = load_release(release_ref)
    commit = _git_commit("HEAD")[:12]
    engine = (
        dict(release.manifest["engine"])
        if pinned
        else {"tag": commit, "api": {"version": 1}}
    )
    commit = engine["tag"]
    supplied_image = os.environ.get("NIKA_CI_ENGINE_IMAGE", "").strip()
    if pinned and supplied_image:
        raise ValueError("A pinned-release test cannot override its engine image")
    if supplied_image:
        assert image_id(supplied_image), (
            f"Engine image {supplied_image!r} is not loaded"
        )
        engine["image"] = supplied_image
    built = not supplied_image and image_id(f"nika/benchmark-engine:{commit}") is None
    image = resolve_engine_image(engine)
    try:
        release_dir = tmp_path / "releases" / release.version
        shutil.copytree(RELEASES_DIR / release.version, release_dir)
        manifest = yaml.safe_load((release_dir / "RELEASE.yaml").read_text())
        if not pinned:
            manifest["engine"] = {**engine, "image": image}
        (release_dir / "RELEASE.yaml").write_text(yaml.safe_dump(manifest))
        with monkeypatch.context() as release_patch:
            release_patch.setattr(
                "nika.workflows.benchmark.release.RELEASES_DIR", tmp_path / "releases"
            )
            yield commit, image
    finally:
        if built and not pinned:
            subprocess.run(["docker", "image", "rm", image], check=False)
