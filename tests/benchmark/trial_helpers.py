"""Shared helpers for benchmark trial tests."""

from __future__ import annotations

import json
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, str]]:
    """Release 0.2.0 pinned to a benchmark engine built from the committed ``HEAD``.

    Yields the engine tag and image. Uncommitted changes are not in the image,
    which is removed afterwards when this call built it.
    """
    from nika.benchmark_engine.container import (
        _git_commit,
        image_id,
        resolve_engine_image,
    )
    from nika.workflows.benchmark.release import RELEASES_DIR

    commit = _git_commit("HEAD")[:12]
    engine = {"tag": commit, "api": {"version": 1}}
    built = image_id(f"nika/benchmark-engine:{commit}") is None
    image = resolve_engine_image(engine)
    try:
        release_dir = tmp_path / "releases" / "0.2.0"
        shutil.copytree(RELEASES_DIR / "0.2.0", release_dir)
        manifest = yaml.safe_load((release_dir / "RELEASE.yaml").read_text())
        manifest["engine"] = {**engine, "image": image}
        (release_dir / "RELEASE.yaml").write_text(yaml.safe_dump(manifest))
        monkeypatch.setattr(
            "nika.workflows.benchmark.release.RELEASES_DIR", tmp_path / "releases"
        )
        yield commit, image
    finally:
        if built:
            subprocess.run(["docker", "image", "rm", image], check=False)
